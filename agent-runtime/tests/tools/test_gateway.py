"""Cycle 5 RED: Tool Gateway tests.

契约来源: blueprint §4.5 + REVISIONS-1.1 R6 —
- 读写权限分离: 未注册/未授权的工具调用直接拒绝（不指望 LLM 遵守 Prompt）
- 参数按 JSON Schema 校验
- 工具调用计数走 BudgetManager（工具侧还有超时与结果大小限制）
- 每次调用产生审计记录
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError

from traceops.runtime.budget import BudgetManager
from traceops.tools.gateway import (
    ToolGateway,
    ToolPermission,
    ToolSpec,
    register_builtin_tools,
)

TS = datetime(2026, 10, 30, 2, 0, 0, tzinfo=UTC)


def query_service_error_rate_impl(service: str, start: str, end: str) -> dict:
    return {"service": service, "error_rate": 0.42, "start": start, "end": end}


def make_tool(**overrides) -> ToolSpec:
    base = dict(
        name="query_service_error_rate",
        description="查询指定服务在时间窗口内的错误率",
        input_schema={
            "type": "object",
            "required": ["service", "start", "end"],
            "properties": {
                "service": {"type": "string"},
                "start": {"type": "string", "format": "date-time"},
                "end": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": False,
        },
        permission=ToolPermission.OBSERVABILITY_READ,
        timeout_seconds=10,
        max_result_bytes=65536,
        implementation=query_service_error_rate_impl,
    )
    base.update(overrides)
    return ToolSpec(**base)


class TestRegistryAndPermission:
    def test_unregistered_tool_rejected(self):
        gw = ToolGateway()
        with pytest.raises(PermissionError, match="not registered"):
            gw.invoke("nonexistent_tool", {}, budget=BudgetManager())

    def test_write_permission_denied_in_readonly_mode(self):
        gw = ToolGateway()
        gw.register(make_tool(permission=ToolPermission.REMEDIATION_WRITE))
        with pytest.raises(PermissionError, match="permission"):
            gw.invoke("query_service_error_rate", {}, budget=BudgetManager(),
                      allowed_permissions=[ToolPermission.OBSERVABILITY_READ])

    def test_read_allowed_in_readonly_mode(self):
        gw = ToolGateway()
        gw.register(make_tool())
        result = gw.invoke(
            "query_service_error_rate",
            {"service": "order-service", "start": "2026-10-30T01:55:00Z", "end": "2026-10-30T02:10:00Z"},
            budget=BudgetManager(),
            allowed_permissions=[ToolPermission.OBSERVABILITY_READ],
        )
        assert result["error_rate"] == 0.42


class TestParameterValidation:
    def test_missing_required_param_rejected(self):
        gw = ToolGateway()
        gw.register(make_tool())
        with pytest.raises(JsonSchemaValidationError):
            gw.invoke("query_service_error_rate", {"service": "x"},
                      budget=BudgetManager(),
                      allowed_permissions=[ToolPermission.OBSERVABILITY_READ])

    def test_extra_param_rejected(self):
        gw = ToolGateway()
        gw.register(make_tool())
        with pytest.raises(JsonSchemaValidationError):
            gw.invoke(
                "query_service_error_rate",
                {"service": "x", "start": "2026-10-30T01:55:00Z", "end": "2026-10-30T02:10:00Z",
                 "evil": "drop table"},
                budget=BudgetManager(),
                allowed_permissions=[ToolPermission.OBSERVABILITY_READ],
            )


class TestBudgetIntegration:
    def test_tool_call_counted_against_budget(self):
        gw = ToolGateway()
        gw.register(make_tool())
        budget = BudgetManager(max_tool_calls=2)
        args = {"service": "x", "start": "2026-10-30T01:55:00Z", "end": "2026-10-30T02:10:00Z"}
        gw.invoke("query_service_error_rate", args, budget=budget,
                  allowed_permissions=[ToolPermission.OBSERVABILITY_READ])
        gw.invoke("query_service_error_rate", args, budget=budget,
                  allowed_permissions=[ToolPermission.OBSERVABILITY_READ])
        with pytest.raises(Exception):  # BudgetExhausted from budget manager
            gw.invoke("query_service_error_rate", args, budget=budget,
                      allowed_permissions=[ToolPermission.OBSERVABILITY_READ])

    def test_failed_validation_does_not_consume_budget(self):
        gw = ToolGateway()
        gw.register(make_tool())
        budget = BudgetManager(max_tool_calls=1)
        with pytest.raises(JsonSchemaValidationError):
            gw.invoke("query_service_error_rate", {}, budget=budget,
                      allowed_permissions=[ToolPermission.OBSERVABILITY_READ])
        # budget still intact
        gw.invoke("query_service_error_rate",
                  {"service": "x", "start": "2026-10-30T01:55:00Z", "end": "2026-10-30T02:10:00Z"},
                  budget=budget, allowed_permissions=[ToolPermission.OBSERVABILITY_READ])


class TestAudit:
    def test_audit_records_success_and_failure(self):
        gw = ToolGateway()
        gw.register(make_tool())
        budget = BudgetManager()
        ap = [ToolPermission.OBSERVABILITY_READ]
        good_args = {"service": "x", "start": "2026-10-30T01:55:00Z", "end": "2026-10-30T02:10:00Z"}
        gw.invoke("query_service_error_rate", good_args, budget=budget, allowed_permissions=ap)
        with pytest.raises(JsonSchemaValidationError):
            gw.invoke("query_service_error_rate", {}, budget=budget, allowed_permissions=ap)
        audit = gw.audit_log()
        assert len(audit) == 2
        assert audit[0]["tool"] == "query_service_error_rate"
        assert audit[0]["outcome"] == "OK"
        assert audit[1]["outcome"] == "REJECTED_SCHEMA"
        assert audit[0]["timestamp"].tzinfo is not None

    def test_audit_records_permission_denial(self):
        gw = ToolGateway()
        gw.register(make_tool(permission=ToolPermission.REMEDIATION_WRITE))
        with pytest.raises(PermissionError):
            gw.invoke("query_service_error_rate", {}, budget=BudgetManager(),
                      allowed_permissions=[ToolPermission.OBSERVABILITY_READ])
        audit = gw.audit_log()
        assert audit[0]["outcome"] == "DENIED_PERMISSION"


class TestBuiltinTools:
    def test_builtin_registry_contains_observability_tools(self):
        gw = ToolGateway()
        register_builtin_tools(gw)
        names = gw.tool_names()
        assert "query_service_error_rate" in names
        assert "query_service_logs" in names
        assert "query_service_traces" in names
        assert "query_metric_series" in names

    def test_builtin_tool_never_has_write_permission(self):
        gw = ToolGateway()
        register_builtin_tools(gw)
        for name in gw.tool_names():
            assert gw.get_tool(name).permission == ToolPermission.OBSERVABILITY_READ
