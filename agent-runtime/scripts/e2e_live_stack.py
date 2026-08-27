"""对真实观测栈跑完整调查（不是假服务器）。

用法：
    PYTHONPATH=agent-runtime .venv/bin/python -m scripts.e2e_live_stack
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import RunContext
from traceops.runtime.budget import BudgetManager
from traceops.runtime.worker import (
    InMemoryEventSink, InMemoryEventSource, InvestigationWorker, derive_request_from_event,
)
from traceops.tools.adapters import DatasourceAdapters, LokiAdapter, PrometheusAdapter, TempoAdapter
from traceops.tools.gateway import ToolGateway, register_builtin_tools
from traceops.topology.graph import ServiceTopology

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
LOKI = "http://localhost:3100"
TEMPO = "http://localhost:3200"
PROM = "http://localhost:9090"


def push_log(trace_id: str, service: str, message: str) -> None:
    now = str(int(time.time() * 1e9))
    # trace_id 只放在日志行内容里，**不**用 structured metadata：
    # 实测 Loki 会把 structured metadata 的键提升为标签，造成高基数（违反 R10）。
    payload = {"streams": [{"stream": {"service": service, "level": "error"},
                            "values": [[now, f"{message} trace_id={trace_id}"]]}]}
    urllib.request.urlopen(urllib.request.Request(
        f"{LOKI}/loki/api/v1/push", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=10).read()


def push_trace(trace_id: str, service: str, name: str, duration_ms: int) -> None:
    import base64
    now_ns = int(time.time() * 1e9)
    # 实测：Tempo 2.5 的 OTLP/HTTP-JSON 端点接受**十六进制**，
    # 传 base64 会被拒（"invalid length for ID"）。虽然 OTLP 规范写的是 base64。
    span = {
        "traceId": trace_id,
        "spanId": "00f067aa0ba902b7",
        "name": name,
        "kind": "SPAN_KIND_CLIENT",
        "startTimeUnixNano": str(now_ns - duration_ms * 1_000_000),
        "endTimeUnixNano": str(now_ns),
        "status": {"code": "STATUS_CODE_ERROR", "message": "pool exhausted"},
    }
    payload = {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"spans": [span]}]}]}
    urllib.request.urlopen(urllib.request.Request(
        f"http://localhost:4318/v1/traces", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=10).read()


def validate(message: dict, schema_rel: str) -> list[str]:
    docs, resources = {}, []
    for rel in ("common/event-envelope.schema.json", "common/time-range.schema.json", schema_rel):
        doc = json.loads((CONTRACTS / rel).read_text())
        docs[rel] = doc
        resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)))
    return [e.message for e in sorted(
        Draft202012Validator(docs[schema_rel], registry=Registry().with_resources(resources))
        .iter_errors(message), key=lambda e: list(e.path))]


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
        if not ok:
            failures.append(f"{name}: {detail}")

    print("TraceOps e2e against LIVE observability stack")
    print("-" * 66)

    # 必须是真实十六进制："2"*32 是 32 个字符=16 字节，但 Tempo 校验的是解码后长度
    # 程序生成确定的 32 位十六进制（16 字节）；手写容易长度出错
    trace_id = "".join(f"{i:02x}" for i in range(16))
    print("> 注入测试数据（真实 Loki + Tempo）...")
    push_log(trace_id, "order-service", "ERROR redis connection timeout: pool exhausted")
    push_trace(trace_id, "redis", "redis.command", 2100)
    time.sleep(6)
    print("> 注入完成\n")

    now = datetime.now(UTC)
    adapters = DatasourceAdapters(
        prometheus=PrometheusAdapter(PROM), loki=LokiAdapter(LOKI), tempo=TempoAdapter(TEMPO))
    topology = ServiceTopology.from_edges([("order-service", "redis")])
    store = EvidenceStore()

    event = {
        "id": "live-1",
        "payload": {
            "envelope": {
                "event_id": "evt-22222222-3333-4444-5555-666666666666",
                "event_type": "investigation.requested", "schema_version": "1.0",
                "occurred_at": now.isoformat(), "correlation_id": "inc-live",
                "causation_id": None, "aggregate_id": "run-live",
                "aggregate_version": 1, "producer": "control-plane",
            },
            "payload": {
                "investigation_run_id": "run-live", "incident_id": "inc-live",
                "service_hint": "order-service", "environment": "live",
                "time_range": {"start": (now - timedelta(minutes=10)).isoformat(),
                               "end": now.isoformat()},
                "config_version": "cfg-live",
                "topology_edges": [["order-service", "redis"]],
            },
        },
    }
    check("event matches contract",
          not validate(event["payload"], "events/v1/investigation-requested.schema.json"))

    source = InMemoryEventSource()
    sink = InMemoryEventSink()
    source.push(event)

    def build_context(request) -> RunContext:
        gateway = ToolGateway()
        register_builtin_tools(gateway, adapters)
        return RunContext(store=store, adapters=adapters,
                          budget=BudgetManager(max_tool_calls=40),
                          gateway=gateway, topology=topology, scope_depth=1)

    worker = InvestigationWorker(source=source, sink=sink,
                                 derive_request=derive_request_from_event,
                                 build_context=build_context)
    stats = worker.poll_once()

    check("worker ran", stats.processed == 1, str(stats))
    rows = store.list(incident_id="inc-live")
    check("evidence collected from LIVE stack", len(rows) >= 1, f"{len(rows)} rows")

    types = {e.evidence_type.value for e in rows}
    check("log evidence from live Loki", "log" in types, str(sorted(types)))
    check("trace evidence from live Tempo", "trace" in types, str(sorted(types)))

    # 关键：trace 证据必须有正确的 trace_id（验证 OTLP base64 解码）
    # Tempo 会保留历史 trace，所以必须找"本次注入的那个"，不能取第一个
    traces = [e for e in rows if e.evidence_type.value == "trace"]
    ours = next((e for e in traces if e.trace_id == trace_id), None)
    check("trace evidence for this run was collected", ours is not None,
          f"got trace_ids {[e.trace_id for e in traces]}")
    if ours is not None:
        check("normalizer emits hex trace_id (matches Loki log format)",
              ours.trace_id == trace_id, f"got {ours.trace_id}")
        spans = ours.normalized_data.get("spans", [])
        check("span duration computed from nano start/end",
              spans and spans[0].get("duration_us") == 2_100_000,
              str(spans[0].get("duration_us") if spans else None))

    completed = [m for m in sink.messages
                 if m["envelope"]["event_type"] == "investigation.completed"]
    check("completed event emitted", len(completed) == 1)
    if completed:
        payload = completed[0]["payload"]
        check("completed event valid",
              not validate(completed[0], "events/v1/investigation-completed.schema.json"))
        print(f"\n  结论: {payload['diagnosis_status']}")
        for c in payload["root_cause_candidates"][:3]:
            print(f"    rank={c['rank']} {c['candidate_service']}/{c['fault_type']}")

    print("-" * 66)
    if failures:
        print(f"RESULT: FAILED ({len(failures)})")
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS — live stack path healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
