"""两层采集测试：调查范围 vs 影响范围。

## 冲突与解法

调查范围只向下游（避免把调用方当成根因）。
但传播链需要上游证据（症状也要能出现在链上）。

硬把上游塞进调查范围 → 假设生成会产生错误候选（症状被当根因）。
完全不采上游 → 传播链断了，影响范围靠拓扑猜（等于编故事）。

解法：**两层采集**
1. `investigation` 层：service_hint + 下游依赖 → 进假设生成与排序
2. `impact` 层：上游调用方（blast radius）→ 只进传播链与影响范围，**不进候选**

两层证据都是真实证据，都进 Evidence Store（可审计），但语义不同。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 30, 10, 6, 0, tzinfo=UTC)


def topology() -> ServiceTopology:
    # api-gateway → order-service → redis
    return ServiceTopology.from_edges([
        ("api-gateway", "order-service"),
        ("order-service", "redis"),
        ("order-service", "mysql"),
    ])


class CascadeAdapters:
    """三服务级联：redis 根因，order-service/api-gateway 症状。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _has_data(self, service: str) -> bool:
        return service in {"redis", "order-service", "api-gateway"}

    def query_service_error_rate(self, *, service, start, end):
        self.calls.append(("error_rate", service))
        if not self._has_data(service):
            raise RuntimeError(f"no data for {service}")
        ts = int(FAULT_START.timestamp())
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": service}, "values": [[ts - 60, "0.01"], [ts, "0.4"]]}]}}

    def query_service_logs(self, *, service, start, end, filter=None):
        self.calls.append(("logs", service))
        messages = {
            "redis": "ERROR redis: connection pool exhausted (max=2)",
            "order-service": "ERROR order-service: redis timeout acquiring connection",
            "api-gateway": "ERROR api-gateway: upstream 502 for /orders",
        }
        if service not in messages:
            raise RuntimeError(f"no logs for {service}")
        ns = int(FAULT_START.timestamp() * 1e9)
        return {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": service, "level": "error"},
             "values": [[str(ns), messages[service]]]}]}}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        self.calls.append(("traces", service))
        if not self._has_data(service):
            raise RuntimeError(f"no traces for {service}")
        offset = {"redis": 0, "order-service": 200_000, "api-gateway": 400_000}[service]
        return {"traces_considered": 1, "batches": [{
            "resource": {"service.name": service},
            "spans": [{"traceID": "a" * 32, "spanID": "1" * 16, "operationName": "redis.command",
                       "startTime": int(FAULT_START.timestamp() * 1e6) + offset,
                       "duration": 2_000_000}]}]}

    def query_metric_series(self, *, service, metric, start, end):
        raise RuntimeError("unused")


def context(adapters=None, *, topo=None, depth=1, impact=True) -> RunContext:
    return RunContext(
        store=EvidenceStore(),
        adapters=adapters or CascadeAdapters(),
        budget=BudgetManager(max_tool_calls=40),
        topology=topo if topo is not None else topology(),
        scope_depth=depth,
        impact_scope=impact,
    )


def request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id="run-2tier", incident_id="inc-2tier",
        environment="demo", service_hint="order-service",
        fault_start=FAULT_START, window_start=WINDOW_START, window_end=WINDOW_END,
    )


class TestTwoTierCollection:
    def test_impact_tier_collects_upstream_symptom(self):
        """上游 api-gateway 必须被采到 —— 否则传播链无法完整。"""
        adapters = CascadeAdapters()
        ctx = context(adapters)

        run_investigation(request(), ctx)

        services_queried = {service for _, service in adapters.calls}
        assert "api-gateway" in services_queried, services_queried

    def test_investigation_tier_still_query_downstream(self):
        adapters = CascadeAdapters()
        ctx = context(adapters)
        run_investigation(request(), ctx)
        services_queried = {service for _, service in adapters.calls}
        assert {"order-service", "redis"} <= services_queried

    def test_impact_tier_can_be_disabled(self):
        adapters = CascadeAdapters()
        ctx = context(adapters, impact=False)
        run_investigation(request(), ctx)
        assert "api-gateway" not in {service for _, service in adapters.calls}

    def test_full_chain_reconstructed(self):
        """关键断言：三跳传播链完整重建（修复 chain=0.00）。"""
        ctx = context()

        result = run_investigation(request(), ctx)

        assert result.report is not None
        chain = [n["service"] for n in result.report.propagation_chain]
        assert chain == ["redis", "order-service", "api-gateway"], chain

    def test_impact_evidence_does_not_create_candidates(self):
        """上游症状不能成为候选根因 —— 这是两层采集存在的理由。"""
        ctx = context()

        result = run_investigation(request(), ctx)

        services = {h.candidate_service for h in result.report.hypotheses}
        assert "api-gateway" not in services, (
            f"api-gateway must stay a symptom, got candidates: {services}")

    def test_root_cause_still_the_downstream_service(self):
        ctx = context()
        result = run_investigation(request(), ctx)
        assert result.report.hypotheses[0].candidate_service == "redis"

    def test_impact_evidence_is_persisted_and_auditable(self):
        """两层证据都要进 Store —— 传播链上的每一跳必须能点回证据。"""
        ctx = context()

        result = run_investigation(request(), ctx)

        services = {ctx.store.get(eid).service_id for eid in result.evidence_ids}
        assert {"redis", "order-service", "api-gateway"} <= services

    def test_chain_nodes_carry_evidence_for_every_hop(self):
        ctx = context()
        result = run_investigation(request(), ctx)
        for node in result.report.propagation_chain:
            assert node["evidence_ids"], f"node {node['service']} has no evidence"

    def test_impact_tier_respects_budget(self):
        """两层采集不能把预算吃干：预算耗尽要如实失败，不能假装完成。"""
        ctx = RunContext(store=EvidenceStore(), adapters=CascadeAdapters(),
                         budget=BudgetManager(max_tool_calls=2),
                         topology=topology(), scope_depth=1, impact_scope=True)
        result = run_investigation(request(), ctx)
        assert result.status == "FAILED"
        assert result.report is None
