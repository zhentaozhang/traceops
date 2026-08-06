#!/usr/bin/env python3
"""TraceOps event contract validation (CI seed).

用法:
    python3 contracts/tests/test_contracts.py

CI 集成: 直接作为 pytest 兼容脚本运行（不依赖 pytest 也可独立执行）。
Java 侧对应测试: control-plane 中的 networknt 校验用例，加载同一批 schema 文件。
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parents[2]  # contracts/
CONTRACTS = ROOT / "contracts"

SCHEMA_PATHS = [
    "common/event-envelope.schema.json",
    "common/time-range.schema.json",
    "events/v1/investigation-requested.schema.json",
    "events/v1/investigation-updated.schema.json",
    "events/v1/investigation-completed.schema.json",
    "events/v1/investigation-failed.schema.json",
]


def _load_registry() -> tuple[Registry, dict]:
    resources = []
    loaded = {}
    for rel in SCHEMA_PATHS:
        s = json.loads((CONTRACTS / rel).read_text())
        loaded[rel] = s
        resources.append((s["$id"], Resource.from_contents(s, default_specification=DRAFT202012)))
    return Registry().with_resources(resources), loaded


REGISTRY, LOADED = _load_registry()


def envelope(etype: str, producer: str = "control-plane") -> dict:
    return {
        "event_id": f"evt-{uuid.uuid4()}",
        "event_type": etype,
        "schema_version": "1.0",
        "occurred_at": "2026-10-30T02:00:00Z",
        "correlation_id": "inc-001",
        "causation_id": None,
        "aggregate_id": "inv-001",
        "aggregate_version": 1,
        "producer": producer,
    }


def _check(msg: dict, schema_path: str, expect_pass: bool) -> None:
    v = Draft202012Validator(LOADED[schema_path], registry=REGISTRY)
    errs = sorted(v.iter_errors(msg), key=lambda e: list(e.path))
    if expect_pass:
        assert not errs, f"expected valid, got: {[str(e.message) for e in errs[:3]]}"
    else:
        assert errs, "expected invalid, but message passed validation"


GOOD_REQUESTED = {
    "envelope": envelope("investigation.requested"),
    "payload": {
        "investigation_run_id": "inv-001",
        "incident_id": "inc-001",
        "service_hint": "order-service",
        "environment": "prod",
        "time_range": {"start": "2026-10-30T01:55:00Z", "end": "2026-10-30T02:10:00Z"},
        "config_version": "cfg-7",
        "priority": "high",
        "budget": {"max_tool_calls": 50, "max_tokens": 100000},
    },
}


def test_requested_minimal_good():
    _check(GOOD_REQUESTED, "events/v1/investigation-requested.schema.json", True)


def test_updated_running():
    _check(
        {
            "envelope": envelope("investigation.updated", producer="agent-runtime"),
            "payload": {
                "investigation_run_id": "inv-001",
                "status": "RUNNING",
                "aggregate_version": 3,
                "current_node": "investigator",
                "progress": {"evidence_count": 7, "tokens_used": 12345, "tool_call_count": 9},
            },
        },
        "events/v1/investigation-updated.schema.json",
        True,
    )


def test_completed_supported():
    _check(
        {
            "envelope": envelope("investigation.completed", producer="agent-runtime"),
            "payload": {
                "investigation_run_id": "inv-001",
                "diagnosis_status": "SUPPORTED",
                "rca_report_ref": "inv-001/report-1",
                "root_cause_candidates": [
                    {
                        "candidate_service": "redis",
                        "candidate_component": "connection-pool",
                        "fault_type": "pool_exhaustion",
                        "rank": 1,
                        "hypothesis_status": "supported",
                    }
                ],
                "evidence_summary": {"total": 12, "metrics": 4, "logs": 5, "traces": 3, "events": 0},
                "usage": {"tool_calls": 21, "tokens": 68000, "rounds": 4, "duration_seconds": 143},
                "abstained": False,
            },
        },
        "events/v1/investigation-completed.schema.json",
        True,
    )


def test_failed_retryable():
    _check(
        {
            "envelope": envelope("investigation.failed", producer="agent-runtime"),
            "payload": {
                "investigation_run_id": "inv-001",
                "terminal_status": "FAILED",
                "failure_reason": "MCP_SERVER_UNAVAILABLE",
                "retryable": True,
                "failed_node": "investigator",
                "checkpoint_id": "ckpt-17",
                "usage_at_failure": {"tool_calls": 8, "tokens": 15000, "rounds": 2},
            },
        },
        "events/v1/investigation-failed.schema.json",
        True,
    )


def test_requested_missing_run_id_rejected():
    _check(
        {
            "envelope": envelope("investigation.requested"),
            "payload": {
                "incident_id": "inc-001",
                "service_hint": "order-service",
                "environment": "prod",
                "time_range": {"start": "2026-10-30T01:55:00Z", "end": "2026-10-30T02:10:00Z"},
                "config_version": "cfg-7",
            },
        },
        "events/v1/investigation-requested.schema.json",
        False,
    )


def test_requested_bad_event_id_rejected():
    msg = {"envelope": {**envelope("investigation.requested"), "event_id": "not-uuid"},
           "payload": GOOD_REQUESTED["payload"]}
    _check(msg, "events/v1/investigation-requested.schema.json", False)


def test_requested_wrong_producer_rejected():
    msg = {"envelope": {**envelope("investigation.requested"), "producer": "agent-runtime"},
           "payload": GOOD_REQUESTED["payload"]}
    _check(msg, "events/v1/investigation-requested.schema.json", False)


def test_updated_illegal_status_rejected():
    _check(
        {
            "envelope": envelope("investigation.updated", producer="agent-runtime"),
            "payload": {"investigation_run_id": "inv-001", "status": "COMPLETED", "aggregate_version": 2},
        },
        "events/v1/investigation-updated.schema.json",
        False,
    )


def test_completed_extra_field_rejected():
    _check(
        {
            "envelope": envelope("investigation.completed", producer="agent-runtime"),
            "payload": {
                "investigation_run_id": "inv-001",
                "diagnosis_status": "SUPPORTED",
                "rca_report_ref": "inv-001/report-1",
                "extra_field": "nope",
            },
        },
        "events/v1/investigation-completed.schema.json",
        False,
    )


def test_failed_non_enum_reason_rejected():
    _check(
        {
            "envelope": envelope("investigation.failed", producer="agent-runtime"),
            "payload": {
                "investigation_run_id": "inv-001",
                "terminal_status": "FAILED",
                "failure_reason": "SOMETHING_VAGUE",
                "retryable": True,
            },
        },
        "events/v1/investigation-failed.schema.json",
        False,
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------- 跨语言 round-trip（S3 的核心验收） ----------------

def test_java_shaped_payload_is_accepted_by_contract():
    """模拟 Java EventMessages 产出的形状（含 scope_depth/service_hint），
    用 Python 侧同一批 schema 校验 —— 这是 S3 双向契约的一半。"""
    import json
    from pathlib import Path

    java_shape = {
        "envelope": {
            "event_id": "evt-" + "0" * 8 + "-1111-2222-3333-" + "4" * 12,
            "event_type": "investigation.requested",
            "schema_version": "1.0",
            "occurred_at": "2026-10-30T10:06:00Z",
            "correlation_id": "inc-001",
            "causation_id": None,
            "aggregate_id": "run-001",
            "aggregate_version": 1,
            "producer": "control-plane",
        },
        "payload": {
            "investigation_run_id": "run-001",
            "incident_id": "inc-001",
            "service_hint": "order-service",
            "environment": "demo",
            "time_range": {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:06:00Z"},
            "config_version": "cfg-2026.10",
            "priority": "high",
            "scope_depth": 1,
            "budget": {"max_tool_calls": 50, "max_rounds": 10, "max_tokens": 100000,
                       "deadline_seconds": 600},
        },
    }
    _check(java_shape, "events/v1/investigation-requested.schema.json", True)


def test_service_hint_is_required_not_optional():
    """反向锁定：service_hint 缺失必须被拒。没有它 Agent 无法启动调查。"""
    msg = {
        "envelope": envelope("investigation.requested"),
        "payload": {
            "investigation_run_id": "run-001",
            "incident_id": "inc-001",
            "environment": "demo",
            "time_range": {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:06:00Z"},
            "config_version": "cfg-1",
        },
    }
    _check(msg, "events/v1/investigation-requested.schema.json", False)


def test_scope_depth_out_of_range_rejected():
    msg = {
        "envelope": envelope("investigation.requested"),
        "payload": {
            "investigation_run_id": "run-001",
            "incident_id": "inc-001",
            "service_hint": "order-service",
            "environment": "demo",
            "time_range": {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:06:00Z"},
            "config_version": "cfg-1",
            "scope_depth": 9,
        },
    }
    _check(msg, "events/v1/investigation-requested.schema.json", False)
