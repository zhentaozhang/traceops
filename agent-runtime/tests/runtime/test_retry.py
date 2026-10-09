"""调查重试分类与进度保留（蓝图 §8.4 / §13.2）。

## 要解决的问题

一次调查失败后，有两种错误做法：
1. **盲目重试**：预算耗尽的调查重跑必然再次耗尽，纯烧钱
2. **永不重试**：MCP 短暂不可用就让事故永远停在 FAILED

正确做法：按失败原因分类决定是否可重试。

## 边界：这里只测分类

**重试的次数、退避和预算上限在控制面**（`InvestigationRetryService.java`），
不在 agent 侧：事故状态机和持久化 outbox 都在控制面，agent 进程重启
就丢了自己那份计数。所以 agent 侧只保留 `is_retryable` 这个分类器，
它必须与 contracts 的 failure_reason 枚举同构。

分工由 `test_retry_ownership.py` 锁住。
"""
from __future__ import annotations

from traceops.runtime.retry import is_retryable


class TestRetryClassification:
    """哪些失败值得重试 —— 这是纯策略，不涉及状态。"""

    def test_budget_exhaustion_is_not_retryable(self):
        """同样的预算再跑必然再次耗尽；重试只是烧钱。"""
        assert is_retryable("BUDGET_EXHAUSTED") is False

    def test_deadline_exceeded_is_not_retryable(self):
        assert is_retryable("DEADLINE_EXCEEDED") is False

    def test_invalid_request_is_not_retryable(self):
        """请求本身非法，重试不会让它变合法。"""
        assert is_retryable("INVALID_REQUEST") is False

    def test_cancelled_by_user_is_not_retryable(self):
        """用户主动取消不该被系统"好心"重试。"""
        assert is_retryable("CANCELLED_BY_USER") is False

    def test_mcp_server_unavailable_is_retryable(self):
        """MCP 短暂不可用是瞬时故障，值得重试。"""
        assert is_retryable("MCP_SERVER_UNAVAILABLE") is True

    def test_model_timeout_is_retryable(self):
        assert is_retryable("MODEL_TIMEOUT") is True

    def test_model_unavailable_is_retryable(self):
        assert is_retryable("MODEL_UNAVAILABLE") is True

    def test_datasource_query_error_is_retryable(self):
        assert is_retryable("DATASOURCE_QUERY_ERROR") is True

    def test_evidence_write_error_is_retryable(self):
        assert is_retryable("EVIDENCE_WRITE_ERROR") is True

    def test_datasource_missing_is_not_retryable(self):
        """数据源根本不存在，重试也不会出现。"""
        assert is_retryable("DATASOURCE_MISSING") is False

    def test_checkpoint_restore_failed_is_retryable(self):
        assert is_retryable("CHECKPOINT_RESTORE_FAILED") is True

    def test_internal_error_is_retryable(self):
        assert is_retryable("INTERNAL_ERROR") is True

    def test_unknown_reason_defaults_to_not_retryable(self):
        """未知原因默认不重试：宁可停下等人，也不要盲目循环。"""
        assert is_retryable("SOMETHING_NEW") is False

    def test_the_two_lists_do_not_overlap(self):
        """两个清单是同一枚举的互补划分。

        如果有人往可重试清单里加了一个已经在不可重试清单里的值，
        这次重叠会立刻暴露 —— 否则分歧只会出现在"谁先判"这种隐式约定上。
        """
        from traceops.runtime.retry import NON_RETRYABLE_REASONS, RETRYABLE_REASONS

        assert not (RETRYABLE_REASONS & NON_RETRYABLE_REASONS)

    def test_the_lists_cover_every_contract_failure_reason(self):
        """分类必须覆盖契约里的全部 failure_reason。

        漏掉一个 = 那个原因会走"未知 → 不重试"这条默认路径，
        看起来安全，实际上是把一个明确的分类变成了隐式的分类。
        """
        import json
        import pathlib

        from traceops.runtime.retry import NON_RETRYABLE_REASONS, RETRYABLE_REASONS

        schema_path = (
            pathlib.Path(__file__).resolve().parents[3]
            / "contracts" / "events" / "v1" / "investigation-failed.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        contract_reasons = set(
            schema["properties"]["payload"]["properties"]["failure_reason"]["enum"]
        )

        assert contract_reasons == (RETRYABLE_REASONS | NON_RETRYABLE_REASONS), (
            "契约里的 failure_reason 与 agent 侧的分类清单不一致："
            f"契约独有 {contract_reasons - (RETRYABLE_REASONS | NON_RETRYABLE_REASONS)}，"
            f"清单独有 {(RETRYABLE_REASONS | NON_RETRYABLE_REASONS) - contract_reasons}")


class TestRetryKeepsCheckpoint:
    """重试必须继承进度，不能从头再跑。"""

    def test_checkpoint_id_survives_failure(self):
        """失败时保留 checkpoint，重试从断点继续（不是从 plan 重来）。"""
        from traceops.runtime.investigation_run import InvestigationRun

        run = InvestigationRun.open(incident_id="inc-001")
        run.queue()
        run.start(node="collect")
        run.record_checkpoint("ckpt-3")
        run.fail(reason="MCP_SERVER_UNAVAILABLE", node="collect", checkpoint_id="ckpt-3")

        assert run.checkpoint_id == "ckpt-3"

    def test_retry_attempts_are_counted_on_run(self):
        from traceops.runtime.investigation_run import InvestigationRun

        run = InvestigationRun.open(incident_id="inc-001")
        run.queue()
        run.start(node="plan")
        assert run.attempts == 1
        run.fail(reason="MODEL_TIMEOUT", checkpoint_id="ckpt-1")
        # 重试是新的 Run 还是同一个 Run 的下一轮，由编排层决定；
        # 这里只保证 attempts 计数可观测
        assert run.attempts == 1
