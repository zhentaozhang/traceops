"""首轮计划的开销是「服务数 × 数据源数」，预算是常数。

真实数据逼出来的问题：RCAEval 的 Online Boutique 有 13 个被观测到的服务，
13 × 3 = 39 次查询，再加判别回路两次就超过默认 40 的上限 —— 整趟调查
直接 FAILED。这不是"数据查不到"，是"根本没开始"。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest,
    RunContext,
    _plan_items,
)
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology


def _request(service: str = "gateway") -> InvestigationRequest:
    start = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
    return InvestigationRequest(
        investigation_run_id="run-1",
        incident_id="inc-1",
        service_hint=service,
        fault_start=start + timedelta(minutes=10),
        window_start=start,
        window_end=start + timedelta(minutes=15),
        alert_summary="error rate up",
    )


class _Adapters:
    """不做任何 IO；计划只看能力声明。"""

    capabilities = frozenset({"metric_series", "error_rate", "logs", "traces"})


def _ctx(services: int, *, max_tool_calls: int) -> RunContext:
    topology = ServiceTopology.from_edges(
        [("gateway", f"svc-{i:02d}") for i in range(services)])
    return RunContext(
        store=EvidenceStore(),
        adapters=_Adapters(),
        budget=BudgetManager(max_tool_calls=max_tool_calls),
        topology=topology,
        scope_depth=1,
    )


def test_the_plan_cannot_outgrow_the_budget():
    """40 次预算配 13 个服务时正好放得下；预算减半就必须截断并记账。"""
    roomy = _ctx(12, max_tool_calls=40)  # gateway + 12 下游 = 13 个服务
    assert len(_plan_items(_request(), roomy)) == 13 * 3
    assert roomy.skipped_by_budget == 0

    tight = _ctx(12, max_tool_calls=20)
    items = _plan_items(_request(), tight)
    assert len(items) <= 20
    assert tight.skipped_by_budget > 0, "截断必须被记录下来，否则报告会像什么都没漏"


def test_the_alerting_service_is_always_queried_first():
    ctx = _ctx(12, max_tool_calls=20)
    items = _plan_items(_request(), ctx)
    assert {item["service"] for item in items[:3]} == {"gateway"}


def test_a_small_scope_still_gets_a_complete_plan():
    """一个服务的集群不该因为截断而少查一类数据源。"""
    ctx = _ctx(2, max_tool_calls=40)
    items = _plan_items(_request(), ctx)
    assert ctx.skipped_by_budget == 0
    assert len(items) == 3 * 3
