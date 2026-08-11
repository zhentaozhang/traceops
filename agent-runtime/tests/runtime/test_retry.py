"""调查重试与恢复测试（蓝图 §8.4 / §13.2）。

## 要解决的问题

一次调查失败后，有两种错误做法：
1. **盲目重试**：预算耗尽的调查重跑必然再次耗尽，纯烧钱
2. **永不重试**：MCP 短暂不可用就让事故永远停在 FAILED

正确做法：按失败原因分类决定是否可重试，并且重试时**继承已完成的进度**
（checkpoint），而不是从零开始。

`retryable` 的判定必须与 contracts 的 failure_reason 枚举同构。
"""
from __future__ import annotations

import pytest

from traceops.runtime.retry import (
    RetryDecision,
    RetryPolicy,
    RetryTracker,
    is_retryable,
)


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


class TestRetryPolicy:
    def test_max_attempts_enforced(self):
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_attempt(attempt=0, reason="MCP_SERVER_UNAVAILABLE") is True
        assert policy.should_attempt(attempt=1, reason="MCP_SERVER_UNAVAILABLE") is True
        assert policy.should_attempt(attempt=2, reason="MCP_SERVER_UNAVAILABLE") is True
        assert policy.should_attempt(attempt=3, reason="MCP_SERVER_UNAVAILABLE") is False

    def test_non_retryable_reason_stops_immediately(self):
        policy = RetryPolicy(max_attempts=10)
        assert policy.should_attempt(attempt=0, reason="BUDGET_EXHAUSTED") is False

    def test_backoff_grows_with_attempts(self):
        policy = RetryPolicy(max_attempts=5, base_delay_seconds=1.0)
        assert policy.delay_for(0) == 1.0
        assert policy.delay_for(1) == 2.0
        assert policy.delay_for(2) == 4.0

    def test_backoff_is_capped(self):
        """指数退避必须有上限，否则第 10 次要等 512 秒。"""
        policy = RetryPolicy(max_attempts=10, base_delay_seconds=1.0, max_delay_seconds=30.0)
        assert policy.delay_for(10) == 30.0

    def test_deadline_exhausted_blocks_retry(self):
        """整体截止时间已过，即使原因可重试也不再排下一次。"""
        policy = RetryPolicy(max_attempts=5, deadline_seconds=60)
        assert policy.should_attempt(attempt=0, reason="MODEL_TIMEOUT", elapsed_seconds=61) is False


class TestRetryTracker:
    """一次 Run 的重试记账。"""

    def test_records_failed_attempts(self):
        tracker = RetryTracker(RetryPolicy(max_attempts=3))
        tracker.record_failure(reason="MODEL_TIMEOUT")
        tracker.record_failure(reason="MODEL_TIMEOUT")
        assert tracker.failures == 2

    def test_decision_on_success_clears(self):
        tracker = RetryTracker(RetryPolicy(max_attempts=3))
        tracker.record_failure(reason="MODEL_TIMEOUT")
        tracker.record_success()
        decision = tracker.decide(reason="MODEL_TIMEOUT")
        assert decision.should_retry is False
        assert decision.reason == "already succeeded"

    def test_decision_carries_delay(self):
        tracker = RetryTracker(RetryPolicy(max_attempts=3, base_delay_seconds=2.0))
        tracker.record_failure(reason="MODEL_TIMEOUT")
        decision = tracker.decide(reason="MODEL_TIMEOUT")
        assert decision.should_retry is True
        assert decision.delay_seconds == 4.0  # 第 2 次尝试的退避

    def test_exhausted_attempts_gives_terminal_decision(self):
        tracker = RetryTracker(RetryPolicy(max_attempts=2))
        tracker.record_failure(reason="MCP_SERVER_UNAVAILABLE")
        tracker.record_failure(reason="MCP_SERVER_UNAVAILABLE")
        decision = tracker.decide(reason="MCP_SERVER_UNAVAILABLE")
        assert decision.should_retry is False
        assert "attempts exhausted" in decision.reason

    def test_non_retryable_gives_immediate_terminal(self):
        tracker = RetryTracker(RetryPolicy(max_attempts=10))
        decision = tracker.decide(reason="BUDGET_EXHAUSTED")
        assert decision.should_retry is False
        assert "not retryable" in decision.reason

    def test_snapshot_for_persistence(self):
        tracker = RetryTracker(RetryPolicy(max_attempts=3))
        tracker.record_failure(reason="MODEL_TIMEOUT")
        snap = tracker.snapshot()
        assert snap["failures"] == 1
        assert snap["max_attempts"] == 3
        assert snap["last_reason"] == "MODEL_TIMEOUT"


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
