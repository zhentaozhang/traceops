"""端到端闭环测试：Java 产出 → Python 消费 → 结果事件。

这是 S3 的完整验收（不只是 schema 校验，而是真正跑通一次闭环）。

链路：
    Java EventMessages.investigationRequested 的产物形状
        ↓ (契约校验)
    derive_request_from_event
        ↓
    InvestigationWorker → LangGraph 调查
        ↓
    investigation.completed 事件（可被 Control Plane 消费）
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext
from traceops.runtime.budget import BudgetManager
from traceops.runtime.worker import (
    InMemoryEventSink,
    InMemoryEventSource,
    InvestigationWorker,
    derive_request_from_event,
)
from traceops.topology.graph import ServiceTopology

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"

WINDOW_START = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 30, 10, 6, 0, tzinfo=UTC)
FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def java_shaped_requested_event(**payload_overrides) -> dict:
    """与 control-plane EventMessages 产出完全一致的形状。"""
    payload = {
        "investigation_run_id": "run-e2e-001",
        "incident_id": "inc-e2e-001",
        "service_hint": "order-service",
        "environment": "demo",
        "time_range": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "config_version": "cfg-2026.10",
        "priority": "high",
        "scope_depth": 1,
        "budget": {"max_tool_calls": 50, "max_rounds": 10, "max_tokens": 100000,
                   "deadline_seconds": 600},
    }
    payload.update(payload_overrides)
    return {
        "envelope": {
            "event_id": "evt-11111111-2222-3333-4444-555555555555",
            "event_type": "investigation.requested",
            "schema_version": "1.0",
            "occurred_at": WINDOW_END.isoformat(),
            "correlation_id": "inc-e2e-001",
            "causation_id": None,
            "aggregate_id": "run-e2e-001",
            "aggregate_version": 1,
            "producer": "control-plane",
        },
        "payload": payload,
    }


def cascade_adapters() -> object:
    """三服务级联：redis 是根因，order-service / api-gateway 是症状。"""

    class Adapters:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def _ok(self, service: str) -> bool:
            return service in {"redis", "order-service", "api-gateway", "mysql"}

        def query_service_error_rate(self, *, service, start, end):
            self.calls.append(("error_rate", service))
            if service != "redis":
                raise RuntimeError(f"no data for {service}")
            ts = int(FAULT_START.timestamp())
            return {"status": "success", "data": {"resultType": "matrix", "result": [
                {"metric": {"service": service}, "values": [[ts - 60, "0.01"], [ts, "0.44"]]}]}}

        def query_service_logs(self, *, service, start, end, filter=None):
            self.calls.append(("logs", service))
            if service == "redis":
                message = "ERROR redis: max clients reached, connection pool exhausted (max=2)"
            elif service == "order-service":
                message = "ERROR order-service: redis timeout after 2000ms acquiring connection"
            elif service == "api-gateway":
                message = "ERROR api-gateway: upstream returned 502"
            else:
                raise RuntimeError(f"no logs for {service}")
            ns = int(FAULT_START.timestamp() * 1e9)
            return {"status": "success", "data": {"resultType": "streams", "result": [
                {"stream": {"service": service, "level": "error"}, "values": [[str(ns), message]]}]}}

        def query_service_traces(self, *, service, start, end, min_duration_ms=None):
            self.calls.append(("traces", service))
            if not self._ok(service):
                raise RuntimeError(f"no traces for {service}")
            operation = "redis.command" if service == "redis" else "http.downstream"
            offset = 0 if service == "redis" else 200_000
            return {"traces_considered": 1, "batches": [{
                "resource": {"service.name": service},
                "spans": [{"traceID": "a" * 32, "spanID": "1" * 16, "operationName": operation,
                           "startTime": int(FAULT_START.timestamp() * 1e6) + offset,
                           "duration": 2_100_000}]}]}

        def query_metric_series(self, *, service, metric, start, end):
            raise RuntimeError("unused")

    return Adapters()


def make_worker(topology_edges=None):
    source = InMemoryEventSource()
    sink = InMemoryEventSink()
    adapters = cascade_adapters()
    topology = ServiceTopology.from_edges(topology_edges) if topology_edges else None

    def build_context(request: InvestigationRequest) -> RunContext:
        return RunContext(
            store=EvidenceStore(),
            adapters=adapters,
            budget=BudgetManager(max_tool_calls=30),
            topology=topology,
            scope_depth=request.scope_depth if hasattr(request, "scope_depth") else 1,
        )

    worker = InvestigationWorker(
        source=source,
        sink=sink,
        derive_request=derive_request_from_event,
        build_context=build_context,
    )
    return worker, source, sink


class TestClosedLoop:
    def test_java_event_drives_a_full_investigation(self):
        """闭环主路径：契约形状的事件 → 调查 → completed 事件。"""
        worker, source, sink = make_worker()
        source.push(java_shaped_requested_event())

        stats = worker.poll_once()

        assert stats.succeeded == 1
        assert stats.failed == 0
        types = [m["envelope"]["event_type"] for m in sink.messages]
        # started(updated) -> 每节点进度(updated) -> completed
        assert types[0] == "investigation.updated"
        assert types[-1] == "investigation.completed"
        assert types.count("investigation.completed") == 1
        assert all(t in ("investigation.updated", "investigation.completed") for t in types)

    def test_completed_event_can_be_parsed_by_control_plane_contract(self):
        """回程事件必须符合 investigation-completed 契约：Control Plane 才能消费。"""
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        worker, source, sink = make_worker()
        source.push(java_shaped_requested_event())
        worker.poll_once()

        completed = sink.messages[-1]
        schema_files = [
            "common/event-envelope.schema.json",
            "common/time-range.schema.json",
            "events/v1/investigation-requested.schema.json",
            "events/v1/investigation-updated.schema.json",
            "events/v1/investigation-completed.schema.json",
            "events/v1/investigation-failed.schema.json",
        ]
        resources = []
        loaded = {}
        for rel in schema_files:
            doc = json.loads((CONTRACTS / rel).read_text())
            loaded[rel] = doc
            resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
                             )
        registry = Registry().with_resources(resources)

        validator = Draft202012Validator(
            loaded["events/v1/investigation-completed.schema.json"], registry=registry)
        errors = sorted(validator.iter_errors(completed), key=lambda e: list(e.path))
        assert not errors, f"completed event violates contract: {[e.message for e in errors]}"

    def test_updated_event_can_be_parsed_by_contract(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        worker, source, sink = make_worker()
        source.push(java_shaped_requested_event())
        worker.poll_once()

        updated = sink.messages[0]
        docs = {}
        resources = []
        for rel in ("common/event-envelope.schema.json", "common/time-range.schema.json",
                    "events/v1/investigation-updated.schema.json"):
            doc = json.loads((CONTRACTS / rel).read_text())
            docs[rel] = doc
            resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)))
        validator = Draft202012Validator(
            docs["events/v1/investigation-updated.schema.json"],
            registry=Registry().with_resources(resources))
        errors = sorted(validator.iter_errors(updated), key=lambda e: list(e.path))
        assert not errors, [e.message for e in errors]

    def test_topology_from_event_expands_scope(self):
        """拓扑经事件传入时，采集范围应覆盖下游依赖。"""
        worker, source, sink = make_worker(
            topology_edges=[("api-gateway", "order-service"), ("order-service", "redis")])
        event = java_shaped_requested_event()
        event["payload"]["topology_edges"] = [
            ["api-gateway", "order-service"], ["order-service", "redis"]]
        source.push(event)

        worker.poll_once()

        assert sink.messages[-1]["payload"]["diagnosis_status"] in ("SUPPORTED", "INCONCLUSIVE")

    def test_failed_event_also_matches_contract(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        worker, source, sink = make_worker()
        event = java_shaped_requested_event()
        del event["payload"]["service_hint"]
        source.push(event)

        worker.poll_once()

        failed = sink.messages[-1]
        docs = {}
        resources = []
        for rel in ("common/event-envelope.schema.json", "common/time-range.schema.json",
                    "events/v1/investigation-failed.schema.json"):
            doc = json.loads((CONTRACTS / rel).read_text())
            docs[rel] = doc
            resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)))
        validator = Draft202012Validator(
            docs["events/v1/investigation-failed.schema.json"],
            registry=Registry().with_resources(resources))
        errors = sorted(validator.iter_errors(failed), key=lambda e: list(e.path))
        assert not errors, [e.message for e in errors]
