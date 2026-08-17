"""双向契约验证：Python 的真实输出必须能被 Java 的解析器吃下。

## 为什么这道关卡必须存在

现有的闭环测试只做到"Python 产出的消息符合 JSON Schema"。这不够：
Java 侧不是用 JSON Schema 解析的，而是用手写的 `InvestigationResult.fromJson`
（严格模式：缺字段、枚举越界都抛异常）。

两者有分叉的可能：schema 允许某字段缺失，Java 的 requiredText 却要求必填；
或 schema 的枚举值 Java 不认识。这种分叉只有真正把 Python 的输出喂给
Java 的解析规则才能发现。

## 这个测试怎么做到"不启动 JVM"也能验证

把 Java 解析器的**必填字段约束**用 Python 表达出来（从 Java 源码逐条抄录），
然后断言 Python 的真实产出满足这些约束。它不是替代 JVM 测试，而是让
"Python 侧改动破坏 Java 侧"在 Python 测试套件里立刻失败，而不是等 CI 跑完
两个技术栈才发现。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
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

FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 30, 10, 6, 0, tzinfo=UTC)

# 以下常量逐条对应 Java InvestigationResult.fromJson 的严格解析约束。
# 改动这里必须同时改 Java 那边，否则这条断言就失去意义。
JAVA_REQUIRED_ENVELOPE_FIELDS = (
    "event_id", "event_type", "schema_version", "occurred_at",
    "correlation_id", "producer",
)
JAVA_REQUIRED_COMPLETED_PAYLOAD_FIELDS = (
    "investigation_run_id", "diagnosis_status", "rca_report_ref",
)
JAVA_KNOWN_DIAGNOSIS_STATUSES = {"SUPPORTED", "REFUTED", "INCONCLUSIVE"}
JAVA_KNOWN_FAILURE_REASONS = {
    "BUDGET_EXHAUSTED", "DEADLINE_EXCEEDED", "MODEL_UNAVAILABLE", "MODEL_TIMEOUT",
    "MCP_SERVER_UNAVAILABLE", "DATASOURCE_QUERY_ERROR", "DATASOURCE_MISSING",
    "EVIDENCE_WRITE_ERROR", "CHECKPOINT_RESTORE_FAILED", "INVALID_REQUEST",
    "CANCELLED_BY_USER", "INTERNAL_ERROR",
}


class Adapters:
    """有数据的数据源，让调查真的产出 SUPPORTED 结论。"""

    def query_service_error_rate(self, *, service, start, end):
        ts = int(FAULT_START.timestamp())
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": service}, "values": [[ts - 60, "0.01"], [ts, "0.44"]]}]}}

    def query_service_logs(self, *, service, start, end, filter=None):
        messages = {
            "redis": "ERROR redis: connection pool exhausted (max=2)",
            "order-service": "ERROR order-service: redis timeout",
            "api-gateway": "ERROR api-gateway: upstream 502",
        }
        ns = int(FAULT_START.timestamp() * 1e9)
        message = messages.get(service, "ERROR something broke")
        return {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": service, "level": "error"},
             "values": [[str(ns), message]]}]}}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        offset = {"redis": 0, "order-service": 200_000, "api-gateway": 400_000}.get(service, 0)
        return {"traces_considered": 1, "batches": [{
            "resource": {"service.name": service},
            "spans": [{"traceID": "a" * 32, "spanID": "1" * 16,
                       "operationName": "redis.command",
                       "startTime": int(FAULT_START.timestamp() * 1e6) + offset,
                       "duration": 2_000_000}]}]}

    def query_metric_series(self, *, service, metric, start, end):
        raise RuntimeError("unused")


def run_worker(event: dict) -> list[dict]:
    store = EvidenceStore()
    topology = ServiceTopology.from_edges(
        [("api-gateway", "order-service"), ("order-service", "redis")])
    source = InMemoryEventSource()
    sink = InMemoryEventSink()
    source.push(event)
    worker = InvestigationWorker(
        source=source, sink=sink, derive_request=derive_request_from_event,
        build_context=lambda r: RunContext(
            store=store, adapters=Adapters(), budget=BudgetManager(max_tool_calls=40),
            topology=topology, scope_depth=1),
    )
    worker.poll_once()
    return sink.messages


def requested_event(run_id: str = "run-bidir") -> dict:
    return {
        "id": "1-0",
        "payload": {
            "envelope": {
                "event_id": "evt-11111111-2222-3333-4444-555555555555",
                "event_type": "investigation.requested",
                "schema_version": "1.0",
                "occurred_at": WINDOW_END.isoformat(),
                "correlation_id": "inc-bidir",
                "causation_id": None,
                "aggregate_id": run_id,
                "aggregate_version": 1,
                "producer": "control-plane",
            },
            "payload": {
                "investigation_run_id": run_id,
                "incident_id": "inc-bidir",
                "service_hint": "order-service",
                "environment": "demo",
                "time_range": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
                "config_version": "cfg-1",
            },
        },
    }


class TestCompletedEventMeetsJavaParserContract:
    def test_envelope_has_every_field_java_requires(self):
        completed = [m for m in run_worker(requested_event())
                     if m["envelope"]["event_type"] == "investigation.completed"]
        assert completed, "expected a completed event"
        envelope = completed[0]["envelope"]
        missing = [f for f in JAVA_REQUIRED_ENVELOPE_FIELDS if f not in envelope]
        assert not missing, f"Java requires these envelope fields: {missing}"

    def test_payload_has_every_field_java_requires(self):
        completed = [m for m in run_worker(requested_event())
                     if m["envelope"]["event_type"] == "investigation.completed"][0]
        payload = completed["payload"]
        missing = [f for f in JAVA_REQUIRED_COMPLETED_PAYLOAD_FIELDS if f not in payload]
        assert not missing, f"Java requires these payload fields: {missing}"

    def test_diagnosis_status_is_a_known_java_enum(self):
        completed = [m for m in run_worker(requested_event())
                     if m["envelope"]["event_type"] == "investigation.completed"][0]
        status = completed["payload"]["diagnosis_status"]
        assert status in JAVA_KNOWN_DIAGNOSIS_STATUSES, (
            f"Java would throw on unknown diagnosis_status: {status}")

    def test_producer_and_event_type_are_correct(self):
        completed = [m for m in run_worker(requested_event())
                     if m["envelope"]["event_type"] == "investigation.completed"][0]
        envelope = completed["envelope"]
        # Java 的 verifyIdentity 只接受 agent-runtime 产的调查结果
        assert envelope["producer"] == "agent-runtime"
        assert envelope["event_type"] == "investigation.completed"

    def test_incident_id_comes_from_correlation_id(self):
        """Java 从信封 correlation_id 取事故 ID（payload 里没有 incident_id）。"""
        completed = [m for m in run_worker(requested_event())
                     if m["envelope"]["event_type"] == "investigation.completed"][0]
        assert completed["envelope"]["correlation_id"] == "inc-bidir"
        assert "incident_id" not in completed["payload"], (
            "completed payload must not carry incident_id "
            "(the schema forbids extra properties; Java reads correlation_id)")


class TestFailedEventMeetsJavaParserContract:
    def test_failure_reason_is_a_known_java_enum(self):
        event = requested_event()
        del event["payload"]["payload"]["service_hint"]  # 使派生失败
        messages = run_worker(event)
        failed = [m for m in messages if m["envelope"]["event_type"] == "investigation.failed"]
        assert failed, messages
        reason = failed[0]["payload"]["failure_reason"]
        assert reason in JAVA_KNOWN_FAILURE_REASONS, (
            f"Java would throw on unknown failure_reason: {reason}")

    def test_failed_event_has_java_required_fields(self):
        event = requested_event()
        del event["payload"]["payload"]["service_hint"]
        messages = run_worker(event)
        failed = [m for m in messages if m["envelope"]["event_type"] == "investigation.failed"][0]
        payload = failed["payload"]
        # Java 的 failed 解析要求这三项；缺任一项会抛异常而不是降级
        for field in ("investigation_run_id", "failure_reason", "retryable"):
            assert field in payload, f"missing {field} in failed payload"


class TestContractSchemaAgreement:
    """两个技术栈共用同一批 schema —— 这条确保 schema 本身没被单边改动。"""

    def test_all_python_output_validates_against_shared_schema(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        docs, resources = {}, []
        for rel in ("common/event-envelope.schema.json",
                    "common/time-range.schema.json",
                    "events/v1/investigation-requested.schema.json",
                    "events/v1/investigation-updated.schema.json",
                    "events/v1/investigation-completed.schema.json",
                    "events/v1/investigation-failed.schema.json"):
            doc = json.loads((CONTRACTS / rel).read_text())
            docs[rel] = doc
            resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
                             )
        registry = Registry().with_resources(resources)

        messages = run_worker(requested_event())
        schema_by_type = {
            "investigation.updated": "events/v1/investigation-updated.schema.json",
            "investigation.completed": "events/v1/investigation-completed.schema.json",
            "investigation.failed": "events/v1/investigation-failed.schema.json",
        }
        for message in messages:
            event_type = message["envelope"]["event_type"]
            schema = docs[schema_by_type[event_type]]
            validator = Draft202012Validator(schema, registry=registry)
            errors = sorted(validator.iter_errors(message), key=lambda e: list(e.path))
            assert not errors, f"{event_type}: {[e.message for e in errors]}"
