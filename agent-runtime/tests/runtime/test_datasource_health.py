"""数据源健康必须跨调查累积。

单次调查的 `missing_data` 只能回答"这次少了一块"，回答不了
"这个数据源已经坏了一整天"。而 abstention 的可信度恰恰取决于后者：
日志查询发不出去（工具故障）和日志里确实没有异常（阴性证据）
在报告里长得一模一样，修法却完全相反。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest, RunContext, run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.runtime.datasource_health import (
    PERSISTENT_FAILURE_THRESHOLD,
    DatasourceHealthTracker,
)
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


class TestCounterSemantics:
    def test_a_success_resets_the_consecutive_streak(self):
        tracker = DatasourceHealthTracker()
        tracker.record_failure("loki", "connection refused")
        tracker.record_failure("loki", "connection refused")
        tracker.record_success("loki")

        entry = tracker.summary()[0]
        assert entry["consecutive_failures"] == 0
        assert entry["total_failures"] == 2, "总失败数不清零 —— 它是历史，不是当前状态"
        assert entry["failing_since"] is None

    def test_failing_since_is_the_first_failure_of_the_streak(self):
        tracker = DatasourceHealthTracker()
        start = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
        tracker.record_failure("loki", "boom", at=start)
        tracker.record_failure("loki", "boom", at=start + timedelta(minutes=5))

        entry = tracker.summary()[0]
        assert entry["failing_since"].startswith("2026-10-30T09:00")

    def test_persistent_failure_is_named_after_the_threshold(self):
        tracker = DatasourceHealthTracker()
        for _ in range(PERSISTENT_FAILURE_THRESHOLD - 1):
            tracker.record_failure("loki", "boom")
        assert tracker.persistently_failing() == []

        tracker.record_failure("loki", "boom")
        assert tracker.persistently_failing() == ["loki"]
        assert tracker.was_failing("loki")

    def test_persistently_failing_is_sorted_by_streak(self):
        tracker = DatasourceHealthTracker()
        for _ in range(PERSISTENT_FAILURE_THRESHOLD):
            tracker.record_failure("tempo", "boom")
        for _ in range(PERSISTENT_FAILURE_THRESHOLD + 2):
            tracker.record_failure("loki", "boom")
        assert tracker.persistently_failing() == ["loki", "tempo"]

    def test_summary_puts_the_worst_source_first(self):
        tracker = DatasourceHealthTracker()
        tracker.record_success("prometheus")
        tracker.record_failure("loki", "boom")
        tracker.record_failure("loki", "boom")
        assert tracker.summary()[0]["source_id"] == "loki"


class _FailingLogsAdapters:
    """日志查询必定失败 —— 数据源不可用。"""

    def __init__(self) -> None:
        self.log_calls = 0

    def query_service_error_rate(self, service, start, end):
        return {"status": "success",
                "data": {"resultType": "matrix", "result": []}}

    def query_service_logs(self, service, start, end, filter=None):
        self.log_calls += 1
        raise ConnectionError("loki: connection refused")

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}


class _RecoveringLogsAdapters(_FailingLogsAdapters):
    """第二次查询开始恢复正常。"""

    def query_service_logs(self, service, start, end, filter=None):
        self.log_calls += 1
        if self.log_calls > 1:
            return {"status": "success", "data": {"resultType": "streams", "result": []}}
        raise ConnectionError("loki: connection refused")


def _run(health, adapters, run_id: str):
    from traceops.tools.gateway import ToolGateway, register_builtin_tools

    gateway = ToolGateway()
    register_builtin_tools(gateway, adapters)
    ctx = RunContext(
        store=EvidenceStore(), adapters=adapters,
        budget=BudgetManager(max_tool_calls=40),
        gateway=gateway,
        topology=ServiceTopology.from_edges([("api-gateway", "order-service")]),
        scope_depth=1,
        health=health,
    )
    request = InvestigationRequest(
        investigation_run_id=run_id, incident_id="inc-health",
        environment="demo", service_hint="order-service",
        fault_start=TS, window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
    )
    return run_investigation(request, ctx)


class TestTheStreakSurvivesAcrossRuns:
    def test_a_second_failing_run_reports_a_longer_streak(self):
        health = DatasourceHealthTracker()
        first = _run(health, _FailingLogsAdapters(), "run-1")
        second = _run(health, _FailingLogsAdapters(), "run-2")

        entries = second.report.run_snapshot["datasource_health"]
        assert entries, "报告里没有数据源健康读数，跨调查聚合等于没做"
        failing = [e for e in entries if e["consecutive_failures"] > 0]
        assert failing, entries
        # 第二次调查必须看到**累积**的失败次数，而不是归零重来。
        # 一次调查里同一数据源可能被查多次（每个服务一次），所以比的是
        # "第二轮比第一轮更多"，不是某个具体数字。
        first_streak = max(
            e["consecutive_failures"]
            for e in first.report.run_snapshot["datasource_health"])
        second_streak = max(e["consecutive_failures"] for e in failing)
        assert first_streak >= 1
        assert second_streak > first_streak, (first_streak, second_streak)

    def test_a_recovery_clears_the_streak_on_the_next_run(self):
        health = DatasourceHealthTracker()
        _run(health, _RecoveringLogsAdapters(), "run-before")
        recovery = _run(health, _RecoveringLogsAdapters(), "run-after")

        entries = recovery.report.run_snapshot["datasource_health"]
        assert all(e["consecutive_failures"] == 0 for e in entries), (
            f"恢复后仍在报连续失败：{entries}")

    def test_a_caller_without_a_tracker_gets_no_fabricated_readings(self):
        """没接健康表的调用方（单测/评测）不能凭空出现读数。"""
        result = _run(None, _FailingLogsAdapters(), "run-null")
        assert result.report.run_snapshot["datasource_health"] == []
