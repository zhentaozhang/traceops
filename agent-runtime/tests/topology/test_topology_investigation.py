"""拓扑感知调查（多服务采集）测试。

目标：解锁 F-005 的关键能力 —— 区分"根因服务"与"症状服务"。

设计：
- api-gateway → order-service → redis 的依赖链
- 故障发生在 redis（下游），order-service / api-gateway 只是症状
- 采集按拓扑扩展到多服务，假设生成时用 blame_order 分层
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest,
    RunContext,
    run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 30, 10, 6, 0, tzinfo=UTC)


def topology() -> ServiceTopology:
    return ServiceTopology.from_edges([
        ("api-gateway", "order-service"),
        ("order-service", "redis"),
        ("order-service", "mysql"),
    ])


class MultiServiceAdapters:
    """按 service 返回不同数据，用于验证拓扑扩展是否真的多查了服务。"""

    def __init__(self, *, symptom_services: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.symptom_services = symptom_services or {"api-gateway", "order-service"}
        self.root_service = "redis"

    def _is_root(self, service: str) -> bool:
        return service == self.root_service

    def _is_symptom(self, service: str) -> bool:
        return service in self.symptom_services

    def query_service_error_rate(self, *, service: str, start: str, end: str):
        self.calls.append(("error_rate", service))
        if not (self._is_root(service) or self._is_symptom(service)):
            raise RuntimeError(f"no data for {service}")
        ts = int(FAULT_START.timestamp())
        value = "0.42" if self._is_root(service) else "0.31"
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": service}, "values": [[ts - 60, "0.01"], [ts, value]]}]}}

    def query_service_logs(self, *, service: str, start: str, end: str, filter: str | None = None):
        self.calls.append(("logs", service))
        if self._is_root(service):
            message = "ERROR redis: max connections reached, 2100ms waiting"
        elif self._is_symptom(service):
            message = "ERROR upstream request failed after redis timeout"
        else:
            raise RuntimeError(f"no logs for {service}")
        ts = int(FAULT_START.timestamp() * 1000)
        ns = ts * 1_000_000
        return {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": service, "level": "error"},
             "values": [[str(ns), message]]}]}}

    def query_service_traces(self, *, service: str, start: str, end: str, min_duration_ms: int | None = None):
        self.calls.append(("traces", service))
        if not (self._is_root(service) or self._is_symptom(service)):
            raise RuntimeError(f"no traces for {service}")
        base_us = int(FAULT_START.timestamp() * 1_000_000)
        operation = "redis.command" if self._is_root(service) else "http.downstream"
        # 症状的慢发生在根因之后 200ms（时间先后决定 R-002 方向）
        offset_us = 0 if self._is_root(service) else 200_000
        return {"traces_considered": 1, "batches": [{
            "resource": {"service.name": service},
            "spans": [{
                "traceID": f"{service}-trace".replace("-", "") * 2,
                "spanID": "1111111111111111",
                "operationName": operation,
                "startTime": base_us + offset_us,
                "duration": 2_000_000 if self._is_root(service) else 2_600_000,
            }]}]}

    def query_metric_series(self, *, service: str, metric: str, start: str, end: str):
        self.calls.append(("metric_series", service))
        raise RuntimeError("not used")


def request(**overrides) -> InvestigationRequest:
    payload = {
        "investigation_run_id": "run-topo-001",
        "incident_id": "inc-topo-001",
        "environment": "demo",
        "service_hint": "order-service",
        "fault_start": FAULT_START,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
    }
    payload.update(overrides)
    return InvestigationRequest(**payload)


def context(adapters, topo: ServiceTopology | None = None) -> RunContext:
    return RunContext(
        store=EvidenceStore(),
        adapters=adapters,
        budget=BudgetManager(max_tool_calls=30),
        topology=topo if topo is not None else topology(),
        scope_depth=1,
    )


class TestScopeExpansion:
    def test_collection_covers_hint_and_its_dependencies(self):
        adapters = MultiServiceAdapters()
        ctx = context(adapters)

        run_investigation(request(), ctx)

        queried = {service for _, service in adapters.calls}
        assert "order-service" in queried      # service_hint 本身
        assert "redis" in queried              # 下游依赖（真正的根因所在）
        assert "mysql" in queried              # 另一个下游依赖
        assert "api-gateway" not in queried    # 上游症状不主动展开

    def test_scope_depth_zero_queries_only_hint(self):
        adapters = MultiServiceAdapters()
        ctx = context(adapters)
        ctx.scope_depth = 0

        run_investigation(request(), ctx)

        assert {service for _, service in adapters.calls} == {"order-service"}


class TestRootCauseVersusSymptom:
    def test_root_downstream_service_ranks_above_upstream_symptom(self):
        """redis 是根因（被 order-service 依赖），必须排在症状之前。"""
        adapters = MultiServiceAdapters()
        ctx = context(adapters)

        result = run_investigation(request(), ctx)

        assert result.report is not None
        services = [h.candidate_service for h in result.report.hypotheses]
        assert "redis" in services
        assert services.index("redis") < services.index("order-service")

    def test_report_lists_affected_upstream_services(self):
        """影响范围是反方向：redis 出问题 → order-service / api-gateway 受影响。"""
        adapters = MultiServiceAdapters()
        ctx = context(adapters)

        result = run_investigation(request(), ctx)

        assert result.report is not None
        assert "order-service" in result.report.affected_services
        assert "api-gateway" in result.report.affected_services

    def test_evidence_from_multiple_services_are_collected(self):
        adapters = MultiServiceAdapters()
        ctx = context(adapters)

        result = run_investigation(request(), ctx)

        services = {ctx.store.get(eid).service_id for eid in result.evidence_ids}
        assert {"redis", "order-service"} <= services

    def test_missing_downstream_data_degrades_gracefully(self):
        """下游服务查不到数据时，不应伪造证据，应记录缺失并可能降级。"""
        adapters = MultiServiceAdapters()
        adapters.root_service = "ghost"  # 让 redis 也没数据

        result = context(adapters)
        result = run_investigation(request(), result)

        assert result.report is not None
        assert result.report.missing_data  # 至少记录了部分查询失败
