"""真端到端：真实 HTTP 数据源 + 真实 worker + 完整调查。

## 与 smoke_loop 的区别

smoke_loop 用假的 adapter 对象（直接返回 dict）。这个脚本起**真实的 HTTP 服务**
（假 Prometheus / Loki / Tempo），让 `PrometheusAdapter` 等真实适配器通过真实
socket 请求它们，再由 worker 装配成完整调查。

它验证的是"整条链路能否真的跑起来"，包括：
- 适配器的 URL/参数构造在真实网络上可用
- 网关的参数校验、审计、超限
- 编排的采集 → 假设 → 排序 → 报告
- 契约校验

用法：
    PYTHONPATH=agent-runtime .venv/bin/python -m scripts.e2e_with_fake_stack
"""
from __future__ import annotations

import json
import socket
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import RunContext
from traceops.runtime.budget import BudgetManager
from traceops.runtime.worker import (
    InMemoryEventSink,
    InMemoryEventSource,
    InvestigationWorker,
    derive_request_from_event,
)
from traceops.tools.adapters import DatasourceAdapters, LokiAdapter, PrometheusAdapter, TempoAdapter
from traceops.tools.gateway import ToolGateway, register_builtin_tools
from traceops.topology.graph import ServiceTopology

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


class _Base(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakePrometheus(_Base):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path != "/api/v1/query_range":
            self.send_response(404); self.end_headers(); return
        # 真实地按 query 参数区分：error-rate 才给异常值
        query = params.get("query", [""])[0]
        ts = int(FAULT_START.timestamp())
        if 'status=~"5.."' in query:
            values = [[ts - 60, "0.01"], [ts, "0.44"]]
        else:
            values = [[ts - 60, "0.01"], [ts, "0.02"]]
        self._json({"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": "order-service"}, "values": values}]}})


class FakeLoki(_Base):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path != "/loki/api/v1/query_range":
            self.send_response(404); self.end_headers(); return
        query = params.get("query", [""])[0]
        ns = int(FAULT_START.timestamp() * 1e9)
        if "redis" in query or "order-service" in query:
            line = "ERROR redis timeout - connection pool exhausted"
        else:
            line = "ERROR upstream unavailable"
        self._json({"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": "order-service", "level": "error"},
             "values": [[str(ns), line]]}]}})


class FakeTempo(_Base):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/search":
            trace_id = "a" * 32
            self._json({"traces": [{"traceID": trace_id, "rootServiceName": "order-service",
                                    "durationMs": 2100}]})
            return
        if parsed.path.startswith("/api/traces/"):
            trace_id = parsed.path.rsplit("/", 1)[-1]
            self._json({"batches": [{
                "resource": {"service.name": "order-service"},
                "spans": [{"traceID": trace_id, "spanID": "1" * 16,
                           "operationName": "redis.command",
                           "startTime": int(FAULT_START.timestamp() * 1e6),
                           "duration": 2_100_000}]}]})
            return
        self.send_response(404); self.end_headers()


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _event() -> dict:
    return {
        "id": "e2e-1",
        "payload": {
            "envelope": {
                "event_id": "evt-11111111-2222-3333-4444-555555555555",
                "event_type": "investigation.requested",
                "schema_version": "1.0",
                "occurred_at": "2026-10-30T10:06:00Z",
                "correlation_id": "inc-e2e",
                "causation_id": None,
                "aggregate_id": "run-e2e",
                "aggregate_version": 1,
                "producer": "control-plane",
            },
            "payload": {
                "investigation_run_id": "run-e2e",
                "incident_id": "inc-e2e",
                "service_hint": "order-service",
                "environment": "demo",
                "time_range": {"start": "2026-10-30T10:00:00Z",
                               "end": "2026-10-30T10:06:00Z"},
                "config_version": "cfg-e2e",
                "topology_edges": [["api-gateway", "order-service"],
                                   ["order-service", "redis"]],
                "config_change": {
                    "changed_at": "2026-10-30T10:01:00Z",
                    "component": "redis-connection-pool",
                    "summary": "maxPoolSize reduced 20 -> 2",
                },
            },
        },
    }


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

    print("TraceOps e2e over real HTTP (fake Prometheus/Loki/Tempo)")
    print("-" * 66)

    servers = {
        "prometheus": _serve(FakePrometheus),
        "loki": _serve(FakeLoki),
        "tempo": _serve(FakeTempo),
    }
    try:
        adapters = DatasourceAdapters(
            prometheus=PrometheusAdapter(f"http://127.0.0.1:{servers['prometheus'].server_port}"),
            loki=LokiAdapter(f"http://127.0.0.1:{servers['loki'].server_port}"),
            tempo=TempoAdapter(f"http://127.0.0.1:{servers['tempo'].server_port}"),
        )
        event = _event()
        check("input event matches contract",
              not validate(event["payload"], "events/v1/investigation-requested.schema.json"))

        topology = ServiceTopology.from_edges(
            [(e[0], e[1]) for e in event["payload"]["payload"]["topology_edges"]])
        store = EvidenceStore()
        source = InMemoryEventSource()
        sink = InMemoryEventSink()
        source.push(event)

        def build_context(request) -> RunContext:
            gateway = ToolGateway()
            register_builtin_tools(gateway, adapters)
            return RunContext(store=store, adapters=adapters,
                             budget=BudgetManager(max_tool_calls=40),
                             gateway=gateway, topology=topology, scope_depth=1)

        worker = InvestigationWorker(
            source=source, sink=sink, derive_request=derive_request_from_event,
            build_context=build_context)
        stats = worker.poll_once()

        check("worker succeeded", stats.succeeded == 1, str(stats))
        check("evidence collected over real HTTP", len(store.list(incident_id="inc-e2e")) >= 3,
              f"{len(store.list(incident_id='inc-e2e'))} rows")

        completed = [m for m in sink.messages
                     if m["envelope"]["event_type"] == "investigation.completed"]
        check("completed event emitted", len(completed) == 1)
        if completed:
            payload = completed[0]["payload"]
            check("diagnosis SUPPORTED", payload["diagnosis_status"] == "SUPPORTED",
                  payload["diagnosis_status"])
            top = payload["root_cause_candidates"][0] if payload["root_cause_candidates"] else {}
            check("root cause is the downstream dependency",
                  top.get("candidate_service") == "redis", str(top.get("candidate_service")))
            check("completed event matches contract",
                  not validate(completed[0], "events/v1/investigation-completed.schema.json"))
            check("usage recorded", payload["usage"]["tool_calls"] > 0, str(payload["usage"]))
    finally:
        for server in servers.values():
            server.shutdown()
            server.server_close()

    print("-" * 66)
    if failures:
        print(f"RESULT: FAILED ({len(failures)} issue(s))")
        for failure in failures:
            print("  -", failure)
        return 1
    print("RESULT: PASS — real HTTP path healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
