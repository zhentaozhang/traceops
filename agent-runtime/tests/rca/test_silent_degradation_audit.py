"""静默降级审计：区分**合法跳过**与**危险忽略**。

## 为什么需要这个（十一轮里多数缺陷都是这个形状）

回顾我找到的缺陷，几乎全是"不报错、只是静默变差"：

- 告警批次只取第一条，其余静默丢弃
- 超大工具结果抛异常丢弃
- `rounds` 恒为 0 却当真实用量上报
- `AlertCorrelation` 完整实现但从不被调用
- 配置快照永远默认值

它们的共同点：**测试全绿，生产失真**。这类问题不会自己暴露，
只能靠系统排查。

## 本轮的排查方法

把 `except: pass/continue/return None` 逐个读一遍，按语义分三类：

| 类别 | 判据 | 是否可接受 |
|---|---|---|
| **跳过坏数据点** | 单个畸形条目无法解析，跳过它不影响其余 | ✅ 可接受 |
| **记为缺失并继续** | 失败被写入 `missing_data`/`errors`，可追溯 | ✅ 可接受 |
| **静默忽略** | 失败**不记录**、不影响任何计数器 | ❌ 危险 |

排查结果：14 处 `except` 中 13 处属于前两类，
**1 处属于第三类** —— `ranking.derive_signals` 的矛盾计数不查证据是否存在，
导致支持/矛盾**分母不一致**。已修（见 `test_signal_count_consistency.py`）。

## 本测试的作用

钉住"合法降级"的具体判据，防止未来有人把它改成静默忽略：

- 跳过坏数据点时，**其余数据必须仍然被处理**（不是整批丢弃）
- 失败必须**留下痕迹**（在 missing_data / errors 里能找到）

这两条是"可接受"与"危险"的分界线，且都可测试。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import Evidence, EvidenceType
from traceops.evidence.normalizer import ProfileNormalizer


class TestSkippingBadPointKeepsTheRest:
    """跳过单个畸形数据点是合法的 —— 前提是其余数据仍被处理。"""

    def test_one_bad_frame_does_not_drop_the_whole_profile(self):
        raw = {
            "flamegraph": {
                "names": ["redis.command", "bad.frame", "socket.read"],
                "values": [800_000, "not-a-number", 400_000],
            },
            "metadata": {},
        }
        result = ProfileNormalizer().normalize(raw, max_records=100)
        names = [f["frame"] for f in result["hot_frames"]]
        assert "redis.command" in names, "good frames must survive a bad sibling"
        assert "socket.read" in names
        assert "bad.frame" not in names, "the unparsable frame is skipped, not fabricated"

    def test_all_bad_still_reports_zero_rather_than_lying(self):
        """全部坏掉时应返回 0 条，而不是伪造数据。"""
        raw = {"flamegraph": {"names": ["a", "b"], "values": ["x", "y"]},
               "metadata": {}}
        result = ProfileNormalizer().normalize(raw, max_records=100)
        assert result["total_records"] == 0
        assert result["hot_frames"] == []


class TestFailuresLeaveATrace:
    """失败必须留痕 —— "记入 missing_data" 与 "静默忽略" 的分界线。"""

    def test_collect_records_failure_in_missing_data(self):
        from datetime import UTC, datetime, timedelta

        from traceops.evidence.store import EvidenceStore
        from traceops.orchestration.graph import (
            InvestigationRequest, RunContext, run_investigation,
        )
        from traceops.runtime.budget import BudgetManager

        class BrokenAdapters:
            """每个查询都抛异常 —— 模拟数据源全挂。"""

            def query_service_error_rate(self, *a, **k):
                raise RuntimeError("prometheus unreachable")

            def query_service_logs(self, *a, **k):
                raise RuntimeError("loki unreachable")

            def query_service_traces(self, *a, **k):
                raise RuntimeError("tempo unreachable")

        now = datetime.now(UTC)
        ctx = RunContext(store=EvidenceStore(), adapters=BrokenAdapters(),
                         budget=BudgetManager(max_tool_calls=40))
        result = run_investigation(InvestigationRequest(
            investigation_run_id="r", incident_id="i", environment="demo",
            service_hint="order-service", fault_start=now,
            window_start=now - timedelta(minutes=2), window_end=now,
        ), ctx)

        # 关键：失败必须出现在 missing_data 里，不能静默消失
        assert result.report is not None
        assert any("unreachable" in item for item in result.report.missing_data), (
            f"datasource failures must be recorded, got {result.report.missing_data}")

    def test_all_datasources_down_yields_abstention_not_a_fake_answer(self):
        from datetime import UTC, datetime, timedelta

        from traceops.evidence.store import EvidenceStore
        from traceops.orchestration.graph import (
            InvestigationRequest, RunContext, run_investigation,
        )
        from traceops.runtime.budget import BudgetManager

        class BrokenAdapters:
            def query_service_error_rate(self, *a, **k):
                raise RuntimeError("down")

            def query_service_logs(self, *a, **k):
                raise RuntimeError("down")

            def query_service_traces(self, *a, **k):
                raise RuntimeError("down")

        now = datetime.now(UTC)
        ctx = RunContext(store=EvidenceStore(), adapters=BrokenAdapters(),
                         budget=BudgetManager(max_tool_calls=40))
        result = run_investigation(InvestigationRequest(
            investigation_run_id="r", incident_id="i", environment="demo",
            service_hint="order-service", fault_start=now,
            window_start=now - timedelta(minutes=2), window_end=now,
        ), ctx)

        # 全挂时必须弃权，不能给出 SUPPORTED
        assert result.report.diagnosis_status == "INCONCLUSIVE"
        assert result.report.abstained is True
        assert result.report.root_cause_time is None
