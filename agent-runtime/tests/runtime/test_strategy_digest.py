"""策略指纹与运行快照必须是两件事。

旧实现把它们塞进同一批字符串：

    workflow_version = f"p0-v1;max_tool_calls={max_tool_calls}"
    topology_version = f"file-declared;scope_depth={scope_depth}"

于是同时犯两个方向的错：

- 预算从 50 调到 60 → `workflow_version` 变了 → 两次调查看起来**换了策略**，
  而决策规则一个字没动；
- 把 `ERROR_RATE_THRESHOLD` 从 0.05 改成 0.20 → 一处都没记 → 真正换了策略
  却看不出来。

这个文件把两个方向都钉住：改运行输入只动 snapshot，改策略只动 digest。
"""
from __future__ import annotations

from datetime import UTC, datetime

from traceops.orchestration.graph import InvestigationRequest, RunContext
from traceops.runtime.budget import BudgetManager
from traceops.runtime.strategy import strategy_components, strategy_digest, run_snapshot
from traceops.topology.graph import ServiceTopology


def _ctx(**kwargs) -> RunContext:
    defaults = dict(store=None, adapters=None, budget=BudgetManager(max_tool_calls=10))
    defaults.update(kwargs)
    return RunContext(**defaults)


class TestTheDigestOnlyMovesWhenTheStrategyMoves:
    def test_two_contexts_with_the_same_knobs_agree(self):
        assert _ctx().strategy_digest() == _ctx().strategy_digest()

    def test_a_budget_change_does_not_change_the_strategy(self):
        """预算是运行输入，不是判断标准。"""
        small = _ctx(budget=BudgetManager(max_tool_calls=10)).strategy_digest()
        large = _ctx(budget=BudgetManager(max_tool_calls=500)).strategy_digest()
        assert small == large

    def test_scope_depth_does_not_change_the_strategy(self):
        assert _ctx(scope_depth=1).strategy_digest() == _ctx(scope_depth=3).strategy_digest()

    def test_turning_off_a_gate_changes_the_strategy(self):
        """闸门开关决定"结论会不会被降级" —— 这是策略，不是输入。"""
        on = _ctx(enable_semantic_gate=True).strategy_digest()
        off = _ctx(enable_semantic_gate=False).strategy_digest()
        assert on != off

    def test_shadowing_a_gate_changes_the_strategy(self):
        plain = _ctx().strategy_digest()
        shadowed = _ctx(shadow_gates=frozenset({"ambiguity_gate"})).strategy_digest()
        assert plain != shadowed

    def test_different_gates_shadowed_are_different_strategies(self):
        one = _ctx(shadow_gates=frozenset({"ambiguity_gate"})).strategy_digest()
        other = _ctx(shadow_gates=frozenset({"semantic_gate"})).strategy_digest()
        assert one != other

    def test_the_digest_tracks_the_thresholds(self, monkeypatch):
        """真正会改变结论的旋钮必须进指纹。

        这是这个文件存在的理由：旧实现里 `ERROR_RATE_THRESHOLD`
        改了，快照一处都不变，于是"这次为什么判得不一样"永远查不出来。
        """
        from traceops.orchestration import detectors

        before = _ctx().strategy_digest()
        monkeypatch.setattr(detectors, "ERROR_RATE_THRESHOLD", 0.20)
        assert _ctx().strategy_digest() != before

    def test_the_digest_tracks_the_ranking_weights(self, monkeypatch):
        from traceops.rca import ranking

        before = _ctx().strategy_digest()
        monkeypatch.setattr(ranking, "_W_CONTRADICTION", -30)
        assert _ctx().strategy_digest() != before

    def test_switching_a_model_on_changes_the_strategy(self):
        """挂了模型就换了判断来源，提示词版本随之进指纹。"""
        without = _ctx().strategy_digest()

        class _Model:
            name = "scripted"

        assert _ctx(model=_Model()).strategy_digest() != without

    def test_a_pure_rule_run_records_no_prompt_version(self):
        """纯规则跑记一个提示词版本是假信息。"""
        assert _ctx().prompt_version() is None
        assert _ctx().strategy_components()["prompt"] is None


class TestTheSnapshotOnlyHoldsRunInputs:
    def test_budget_and_depth_land_in_the_snapshot(self):
        snapshot = _ctx(
            budget=BudgetManager(max_tool_calls=7, max_rounds=3, max_tokens=1234),
            scope_depth=2,
        ).run_snapshot()
        assert snapshot["scope_depth"] == 2
        assert snapshot["budget"]["max_tool_calls"] == 7
        assert snapshot["budget"]["max_rounds"] == 3
        assert snapshot["budget"]["max_tokens"] == 1234

    def test_the_alert_text_itself_is_not_copied_into_the_snapshot(self):
        """快照会进报告、报告会进证据库：告警原文不该被复制到第三个地方。"""
        request = InvestigationRequest(
            investigation_run_id="r", incident_id="i", service_hint="s",
            fault_start=datetime(2026, 10, 30, 10, 5, tzinfo=UTC),
            window_start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
            window_end=datetime(2026, 10, 30, 10, 6, tzinfo=UTC),
            alert_summary="cardholder 4242 disputed a charge",
        )
        snapshot = _ctx().run_snapshot(request)
        assert snapshot["alert_summary_present"] is True
        assert "4242" not in repr(snapshot)

    def test_no_topology_is_not_the_same_as_an_empty_topology(self):
        """`None`＝没提供拓扑、退化为单服务；空图＝提供了、但里面没有边。"""
        absent = run_snapshot(topology=None)["topology_digest"]
        empty = run_snapshot(topology=ServiceTopology({}))["topology_digest"]
        assert absent is None
        assert empty is not None

    def test_the_topology_digest_tracks_the_edges(self):
        one = run_snapshot(
            topology=ServiceTopology.from_edges([("a", "b")]))["topology_digest"]
        two = run_snapshot(
            topology=ServiceTopology.from_edges([("a", "b"), ("a", "c")]))["topology_digest"]
        assert one != two

    def test_the_topology_digest_ignores_edge_order(self):
        one = run_snapshot(
            topology=ServiceTopology.from_edges([("a", "b"), ("c", "d")]))["topology_digest"]
        other = run_snapshot(
            topology=ServiceTopology.from_edges([("c", "d"), ("a", "b")]))["topology_digest"]
        assert one == other


class TestTheReportCarriesBoth:
    def _report(self, **overrides):
        from traceops.evidence.store import EvidenceStore
        from traceops.rca.report import build_report

        kwargs = dict(
            investigation_run_id="run-001",
            hypotheses=[],
            links=[],
            evidence=[],
            ranked=[],
            fault_start=datetime(2026, 10, 30, 10, 5, tzinfo=UTC),
            store=EvidenceStore(),
            strategy_digest="abc123",
            run_snapshot={"scope_depth": 1},
        )
        kwargs.update(overrides)
        return build_report(**kwargs)

    def test_build_report_keeps_the_digest_and_the_snapshot(self):
        report = self._report()
        assert report.strategy_digest == "abc123"
        assert report.run_snapshot == {"scope_depth": 1}

    def test_a_report_without_them_does_not_invent_values(self):
        report = self._report(strategy_digest=None, run_snapshot=None)
        assert report.strategy_digest is None
        assert report.run_snapshot == {}


class TestComponentsAreInspectable:
    def test_components_can_be_diffed_by_eye(self):
        """digest 只回答"一不一样"；要回答"差在哪"得看组成。"""
        components = strategy_components()
        assert components["revision"] == "strategy-v1"
        assert components["thresholds"]["error_rate"] > 0
        assert components["ranking"]["contradiction"] < 0
        assert set(components["gates"]) == {"semantic", "contradiction", "shadow"}

    def test_digest_of_components_matches_strategy_digest(self):
        from traceops.runtime.strategy import _digest

        assert _digest(strategy_components()) == strategy_digest()
