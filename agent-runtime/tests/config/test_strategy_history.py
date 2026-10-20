"""策略变更必须留下可查的记录，并且归到"从哪一轮开始换了尺子"。

被替换的缺陷：阈值、排序权重、预算都是模块常量，改了以后**没有任何记录**。
于是同一批报告可能横跨两套策略，而报告本身看不出来 —— 事后问
"上周为什么更准"，只能翻 git log，且无法知道哪次调查对应哪个版本。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from traceops.config.history import (
    CHANGE_HISTORY_LIMIT,
    MAX_CHANGED_PATHS,
    StrategyHistory,
)
from traceops.orchestration import graph as graph_module
from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.evidence.store import EvidenceStore
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _knobs(**overrides):
    from traceops.config.fields import current_knobs

    knobs = current_knobs()
    knobs.update(overrides)
    return knobs


class TestBaseline:
    def test_the_first_observation_is_a_baseline_not_a_change(self):
        history = StrategyHistory()

        change = history.observe(knobs=_knobs(), digest="d1", revision="strategy-v1")

        # 把"从空到有"记成变更，会让第一批报告全都带一条假的策略变更
        assert change is None
        assert history.current_digest == "d1"
        assert history.changes() == []

    def test_an_unchanged_digest_records_nothing(self):
        history = StrategyHistory()
        history.observe(knobs=_knobs(), digest="d1", revision="strategy-v1")

        assert history.observe(knobs=_knobs(), digest="d1", revision="strategy-v1") is None
        assert history.changes() == []


class TestChangedPaths:
    def test_a_changed_knob_is_named_by_dotted_path(self):
        history = StrategyHistory()
        history.observe(knobs=_knobs(**{"thresholds.error_rate": 0.05}), digest="d1",
                        revision="strategy-v1")

        change = history.observe(knobs=_knobs(**{"thresholds.error_rate": 0.2}), digest="d2",
                                 revision="strategy-v1")

        assert change is not None
        # 点分路径才能聚合：自由文本既不能数"哪个旋钮最常被动"，也不能比对两次变更
        assert change.changed_paths == ("thresholds.error_rate",)
        assert change.previous_digest == "d1"
        assert change.digest == "d2"
        assert change.comparable

    def test_several_changes_are_sorted_by_path(self):
        history = StrategyHistory()
        history.observe(knobs=_knobs(), digest="d1", revision="strategy-v1")

        change = history.observe(
            knobs=_knobs(**{"ranking.contradiction": -5, "thresholds.error_rate": 0.2}),
            digest="d2", revision="strategy-v1")

        assert change.changed_paths == ("ranking.contradiction", "thresholds.error_rate")

    def test_a_revision_bump_is_marked_incomparable(self):
        history = StrategyHistory()
        history.observe(knobs=_knobs(), digest="d1", revision="strategy-v1")

        change = history.observe(knobs=_knobs(), digest="d2", revision="strategy-v2")

        # 旋钮集合本身变了：拿两套不同字段的差集去解释结论变化是在编因果
        assert change is not None
        assert not change.comparable
        assert change.changed_paths == ()
        assert change.previous_revision == "strategy-v1"

    def test_a_digest_change_without_knob_changes_is_still_recorded(self):
        """提示词片段内容变了也改变结论，只是不在这批数值旋钮里。"""
        history = StrategyHistory()
        history.observe(knobs=_knobs(), digest="d1", revision="strategy-v1")

        change = history.observe(knobs=_knobs(), digest="d2", revision="strategy-v1")

        assert change is not None
        assert change.changed_paths == ()
        assert change.comparable

    def test_changed_paths_are_capped(self):
        history = StrategyHistory()
        history.observe(knobs={f"k{i}": 0 for i in range(MAX_CHANGED_PATHS + 5)},
                        digest="d1", revision="strategy-v1")

        change = history.observe(knobs={f"k{i}": 1 for i in range(MAX_CHANGED_PATHS + 5)},
                                 digest="d2", revision="strategy-v1")

        # 一次"全量重设"不该把历史撑成几百行
        assert len(change.changed_paths) == MAX_CHANGED_PATHS


class TestBoundedHistory:
    def test_only_the_last_n_changes_are_kept(self):
        history = StrategyHistory(limit=3)
        history.observe(knobs={"a": 0}, digest="d0", revision="strategy-v1")
        for i in range(1, 6):
            history.observe(knobs={"a": i}, digest=f"d{i}", revision="strategy-v1")

        recent = history.changes()
        assert len(recent) == 3
        # 新的在前
        assert [c.digest for c in recent] == ["d5", "d4", "d3"]

    def test_changes_can_be_limited(self):
        history = StrategyHistory(limit=10)
        history.observe(knobs={"a": 0}, digest="d0", revision="strategy-v1")
        for i in range(1, 5):
            history.observe(knobs={"a": i}, digest=f"d{i}", revision="strategy-v1")

        assert [c.digest for c in history.changes(limit=2)] == ["d4", "d3"]

    def test_default_limit_matches_the_documented_value(self):
        history = StrategyHistory()
        history.observe(knobs={"a": 0}, digest="d0", revision="strategy-v1")
        for i in range(1, CHANGE_HISTORY_LIMIT + 10):
            history.observe(knobs={"a": i}, digest=f"d{i}", revision="strategy-v1")

        assert len(history.changes()) == CHANGE_HISTORY_LIMIT

    def test_a_non_positive_limit_is_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            StrategyHistory(limit=0)

    def test_observed_at_is_recorded(self):
        history = StrategyHistory()
        history.observe(knobs={"a": 0}, digest="d0", revision="strategy-v1")
        when = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)

        change = history.observe(knobs={"a": 1}, digest="d1", revision="strategy-v1", at=when)

        assert change.occurred_at == when
        assert change.to_dict()["occurred_at"].startswith("2026-10-30T12:00")


class _QuietAdapters:
    def query_service_error_rate(self, service, start, end):
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    def query_service_logs(self, service, start, end, filter=None):
        return {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}


def _run(history, run_id: str):
    ctx = RunContext(
        store=EvidenceStore(),
        adapters=_QuietAdapters(),
        budget=BudgetManager(max_tool_calls=20),
        topology=ServiceTopology.from_edges([("api-gateway", "order-service")]),
        scope_depth=1,
        strategy_history=history,
    )
    request = InvestigationRequest(
        investigation_run_id=run_id, incident_id="inc-strategy",
        environment="demo", service_hint="order-service",
        fault_start=TS, window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
    )
    return run_investigation(request, ctx)


class TestTheReportSaysWhichRulerWasUsed:
    def test_the_first_run_has_no_change_to_report(self):
        history = StrategyHistory()

        report = _run(history, "run-1").report

        # 首次运行是基线：报一条"策略变了"是假信息
        assert report.run_snapshot["strategy_change"] is None

    def test_a_changed_threshold_shows_up_in_the_next_run(self, monkeypatch):
        from traceops.orchestration import detectors

        history = StrategyHistory()
        _run(history, "run-before")
        monkeypatch.setattr(detectors, "ERROR_RATE_THRESHOLD", 0.42)
        report = _run(history, "run-after").report

        change = report.run_snapshot["strategy_change"]
        assert change is not None, "换阈值之后跑的那一轮，报告里必须能看出来"
        assert change["changed_paths"] == ["thresholds.error_rate"]
        assert change["comparable"] is True

    def test_history_is_untouched_when_no_history_is_wired(self):
        report = _run(None, "run-no-history").report

        # 单测/评测没接历史时不能凭空出现变更记录
        assert report.run_snapshot["strategy_change"] is None
