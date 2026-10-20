"""重试必须重放"上一轮够不到"的那部分，而不是把同一次失败重演一遍。

被替换的缺陷：重试只带"同一个事故、同一个时间窗"，从不带"上轮缺了什么"。
计划顺序固定，预算按调用次数计 —— 上一轮因为 loki 连不上而缺日志，重试仍先查
指标与链路，轮到 loki 时预算又没了。报告会把这次记成"一次新的、同样
INCONCLUSIVE 的调查"，于是同一个原因看起来像两次独立事件。

回路（两端各一半，这里测 Agent 侧）：
    completed.failed_sources  →  控制面记在事故上  →  下一条 requested 带
    retry_of + known_unavailable  →  本轮**先查**那些源
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from traceops.evidence.store import EvidenceStore
from traceops.orchestration import graph as graph_module
from traceops.orchestration.graph import (
    CTX_CONFIG_KEY,
    InvestigationRequest,
    RunContext,
    _prioritise_plan,
    run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.runtime.worker import completed_event
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


# ---------- 1. 顺序是唯一能被预算兑现的东西 ----------

class TestPlanOrdering:
    def test_unavailable_sources_go_first(self):
        plan = [
            {"tool": "a", "source_id": "prometheus"},
            {"tool": "b", "source_id": "loki"},
            {"tool": "c", "source_id": "tempo"},
        ]

        ordered = _prioritise_plan(plan, ["loki"])

        assert [item["source_id"] for item in ordered] == ["loki", "prometheus", "tempo"]

    def test_ordering_keeps_every_item(self):
        plan = [{"tool": "a", "source_id": "prometheus"}, {"tool": "b", "source_id": "loki"}]

        ordered = _prioritise_plan(plan, ["loki"])

        # 只改顺序、不改条数：排序不能凭空多出证据，也不能悄悄少查一个源
        assert sorted(item["tool"] for item in ordered) == ["a", "b"]

    def test_ordering_is_stable_for_repeated_retries(self):
        plan = [
            {"tool": "a", "source_id": "loki"},
            {"tool": "b", "source_id": "loki"},
            {"tool": "c", "source_id": "prometheus"},
        ]

        first = _prioritise_plan(plan, ["loki"])
        second = _prioritise_plan(first, ["loki"])

        # 同一份 plan 重排两次必须一样，否则两次重试之间连"查了什么"都没法对比
        assert [i["tool"] for i in first] == ["a", "b", "c"]
        assert [i["tool"] for i in second] == [i["tool"] for i in first]

    def test_unknown_or_missing_sources_change_nothing(self):
        plan = [{"tool": "a", "source_id": "prometheus"}, {"tool": "b", "source_id": "loki"}]

        assert _prioritise_plan(plan, None) == plan
        assert _prioritise_plan(plan, []) == plan
        # 上一轮缺的源这次根本不在计划里（例如拓扑变了）：不要空转，保持原序
        assert _prioritise_plan(plan, ["postgres"]) == plan


# ---------- 2. 请求字段真的进了计划 ----------

class _QuietAdapters:
    def query_service_error_rate(self, service, start, end):
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    def query_service_logs(self, service, start, end, filter=None):
        return {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}


def _request(**overrides) -> InvestigationRequest:
    base = dict(
        investigation_run_id="run-retry-1",
        incident_id="inc-retry",
        environment="demo",
        service_hint="order-service",
        fault_start=TS,
        window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
    )
    base.update(overrides)
    return InvestigationRequest(**base)


def _context(adapters=None) -> RunContext:
    return RunContext(
        store=EvidenceStore(),
        adapters=adapters or _QuietAdapters(),
        budget=BudgetManager(max_tool_calls=40),
        topology=ServiceTopology.from_edges([("api-gateway", "order-service")]),
        scope_depth=1,
    )


def _plan_for(request: InvestigationRequest) -> list[dict]:
    """直接跑 plan 节点：顺序是要被断言的东西，走完整张图只会把它埋起来。"""
    ctx = _context()
    return graph_module._plan_node(
        {"request": request}, {"configurable": {CTX_CONFIG_KEY: ctx}})["plan"]


class TestTheRequestFieldReachesThePlan:
    def test_known_unavailable_reorders_the_plan(self):
        plan = _plan_for(_request(known_unavailable=["loki"]))

        source_ids = [item["source_id"] for item in plan]
        assert "loki" in source_ids, source_ids
        first_other = next(
            (i for i, s in enumerate(source_ids) if s != "loki"), len(source_ids))
        # 所有 loki 项都必须排在第一个非 loki 项之前
        assert all(s == "loki" for s in source_ids[:first_other]), source_ids

    def test_without_the_field_the_plan_order_is_unchanged(self):
        baseline = [item["tool"] for item in _plan_for(_request())]
        empty = [item["tool"] for item in _plan_for(_request(known_unavailable=[]))]

        # 空列表 ≠ "有一个叫空字符串的源"：它必须与"没提供"完全同序
        assert empty == baseline

    def test_an_unknown_source_does_not_perturb_the_plan(self):
        baseline = [item["tool"] for item in _plan_for(_request())]
        with_unknown = [item["tool"] for item in _plan_for(_request(known_unavailable=["postgres"]))]

        assert with_unknown == baseline


# ---------- 3. 本轮够不到的源要进终态事件 ----------

class _FailingLogs(_QuietAdapters):
    def query_service_logs(self, service, start, end, filter=None):
        raise ConnectionError("loki: connection refused")


class TestFailedSourcesReachTheCompletedEvent:
    def _report(self):
        result = run_investigation(_request(), _context(_FailingLogs()))
        return result.report

    def test_the_report_records_which_sources_failed(self):
        report = self._report()

        assert report.run_snapshot["failed_sources"] == ["loki"]

    def test_the_completed_event_carries_them(self):
        report = self._report()
        run = SimpleNamespace(
            run_id="run-retry-1", incident_id="inc-retry", aggregate_version=1,
            tool_calls=6, tokens_used=100, rounds=2,
            created_at=TS, updated_at=TS + timedelta(seconds=30),
        )

        payload = completed_event(run, report)["payload"]

        # 必须进消息体：控制面靠它决定下一轮先查哪个源，
        # 而 rca_report_ref 指向的存储控制面并不读
        assert payload["failed_sources"] == ["loki"]

    def test_a_healthy_run_reports_an_empty_list_not_a_missing_key(self):
        report = run_investigation(_request(), _context()).report
        run = SimpleNamespace(
            run_id="run-ok", incident_id="inc-ok", aggregate_version=1,
            tool_calls=1, tokens_used=10, rounds=1,
            created_at=TS, updated_at=TS + timedelta(seconds=5),
        )

        payload = completed_event(run, report)["payload"]

        # 空列表是信息（这一轮所有源都够得到），缺失则会被读成"上轮没结论"
        assert payload["failed_sources"] == []
