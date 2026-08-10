"""闭环冒烟脚本：不接真实 Redis / 不接真实 LLM，验证整条链路的接线是否正确。

用途：改完任一组件后，一条命令确认"闭环没断"。代价是几秒，而不是等集成环境。

用法：
    .venv/bin/python -m scripts.smoke_loop
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import RunContext
from traceops.runtime.budget import BudgetManager
from traceops.runtime.deadletter import InMemoryDeadLetterSink
from traceops.runtime.model import ScriptedModel, ModelHypothesis
from traceops.runtime.worker import (
    InMemoryEventSink,
    InMemoryEventSource,
    InvestigationWorker,
    derive_request_from_event,
)
from traceops.topology.graph import ServiceTopology

CONTRACTS = __import__("pathlib").Path(__file__).resolve().parents[2] / "contracts"
FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 30, 10, 6, 0, tzinfo=UTC)


def java_shaped_event() -> dict:
    """与 control-plane EventMessages 产出一致的形状（含 Redis entry 摊平）。"""
    doc = {
        "envelope": {
            "event_id": "evt-11111111-2222-3333-4444-555555555555",
            "event_type": "investigation.requested",
            "schema_version": "1.0",
            "occurred_at": WINDOW_END.isoformat(),
            "correlation_id": "inc-smoke",
            "causation_id": None,
            "aggregate_id": "run-smoke",
            "aggregate_version": 1,
            "producer": "control-plane",
        },
        "payload": {
            "investigation_run_id": "run-smoke",
            "incident_id": "inc-smoke",
            "service_hint": "order-service",
            "environment": "demo",
            "time_range": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
            "config_version": "cfg-smoke",
            "priority": "high",
            "scope_depth": 1,
            # Redis Streams entry：外层 payload 装整份文档（worker 两种形状都接受）
            "topology_edges": [["api-gateway", "order-service"], ["order-service", "redis"]],
            "config_change": {
                "changed_at": "2026-10-30T10:01:00Z",
                "component": "redis-connection-pool",
                "summary": "maxPoolSize reduced 20 -> 2",
            },
        },
    }
    return {"id": "1700000000000-1", "payload": doc}


class SmokeAdapters:
    def query_service_error_rate(self, *, service, start, end):
        ts = int(FAULT_START.timestamp())
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": service}, "values": [[ts - 60, "0.01"], [ts, "0.42"]]}]}}

    def query_service_logs(self, *, service, start, end, filter=None):
        messages = {
            "redis": "ERROR redis: max clients reached, connection pool exhausted (max=2)",
            "order-service": "ERROR order-service: redis timeout after 2000ms",
            "api-gateway": "ERROR api-gateway: upstream 502",
        }
        if service not in messages:
            raise RuntimeError(f"no logs for {service}")
        ns = int(FAULT_START.timestamp() * 1e9)
        return {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": service, "level": "error"},
             "values": [[str(ns), messages[service]]]}]}}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        offset = {"redis": 0, "order-service": 200_000, "api-gateway": 400_000}.get(service, 0)
        return {"traces_considered": 1, "batches": [{
            "resource": {"service.name": service},
            "spans": [{"traceID": "a" * 32, "spanID": "1" * 16, "operationName": "redis.command",
                       "startTime": int(FAULT_START.timestamp() * 1e6) + offset,
                       "duration": 2_000_000}]}]}

    def query_metric_series(self, *, service, metric, start, end):
        raise RuntimeError("unused")


def validate(message: dict, schema_rel: str) -> list[str]:
    docs, resources = {}, []
    for rel in ("common/event-envelope.schema.json", "common/time-range.schema.json", schema_rel):
        doc = json.loads((CONTRACTS / rel).read_text())
        docs[rel] = doc
        resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)))
    validator = Draft202012Validator(docs[schema_rel], registry=Registry().with_resources(resources))
    return [e.message for e in sorted(validator.iter_errors(message), key=lambda e: list(e.path))]


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
        if not ok:
            failures.append(f"{name}: {detail}")

    print("TraceOps closed-loop smoke")
    print("-" * 60)

    # 1. 输入事件符合契约
    event = java_shaped_event()
    errors = validate(event["payload"], "events/v1/investigation-requested.schema.json")
    check("input event matches contract", not errors, str(errors))

    # 2. worker 能派生请求（两种形状）
    from traceops.runtime.worker import InvestigationRequest
    request = derive_request_from_event(event)
    check("request derived from flattened shape", request.investigation_run_id == "run-smoke")
    request2 = derive_request_from_event(event["payload"])
    check("request derived from envelope shape", request2.investigation_run_id == "run-smoke")

    # 3. 调查跑通
    # event["payload"] 是整份文档（Redis entry 摊平形状），拓扑在文档的 payload 里
    doc_payload = event["payload"]["payload"]
    topology = ServiceTopology.from_edges(
        [(edge[0], edge[1]) for edge in doc_payload["topology_edges"]])
    store = EvidenceStore()
    ctx = RunContext(store=store, adapters=SmokeAdapters(),
                     budget=BudgetManager(max_tool_calls=40),
                     topology=topology, scope_depth=1, impact_scope=True,
                     model=ScriptedModel(hypotheses=[
                         ModelHypothesis(candidate_service="order-service",
                                         fault_type="thread_pool_exhaustion",
                                         failure_mechanism="sync calls blocking threads")]))

    source = InMemoryEventSource()
    sink = InMemoryEventSink()
    dead = InMemoryDeadLetterSink()
    source.push(event)

    def build_context(_req: InvestigationRequest) -> RunContext:
        return ctx

    worker = InvestigationWorker(source=source, sink=sink, derive_request=derive_request_from_event,
                                 build_context=build_context, dead_letters=dead)
    stats = worker.poll_once()

    check("worker processed the event", stats.processed == 1 and stats.succeeded == 1, str(stats))
    check("no dead letters", len(dead.records) == 0)
    check("message acknowledged", source.acked == ["1700000000000-1"], str(source.acked))

    # 4. 结论正确
    report = ctx.store  # placeholder to keep flake quiet
    completed = [m for m in sink.messages if m["envelope"]["event_type"] == "investigation.completed"]
    check("completed event emitted", len(completed) == 1)
    if not completed:
        print("\nRESULT: FAILED")
        return 1
    payload = completed[0]["payload"]
    check("diagnosis SUPPORTED", payload["diagnosis_status"] == "SUPPORTED", payload["diagnosis_status"])
    top = payload["root_cause_candidates"][0]
    check("root cause is the downstream dependency", top["candidate_service"] == "redis",
          top["candidate_service"])
    check("no fabricated candidates from impact tier",
          all(c["candidate_service"] != "api-gateway" for c in payload["root_cause_candidates"]))
    check("usage recorded", payload["usage"]["tool_calls"] > 0, str(payload["usage"]))

    # 5. 回程事件符合契约
    errors = validate(completed[0], "events/v1/investigation-completed.schema.json")
    check("completed event matches contract", not errors, str(errors))

    # 6. 幂等
    source.push(event)
    stats2 = worker.poll_once()
    check("duplicate is skipped", stats2.skipped_duplicate == 1, str(stats2))

    print("-" * 60)
    if failures:
        print(f"RESULT: FAILED ({len(failures)} issue(s))")
        for failure in failures:
            print("  -", failure)
        return 1
    print("RESULT: PASS — closed loop healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
