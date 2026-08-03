"""Tool Gateway (blueprint §4.5)。

Agent 不直接调用 MCP Server：所有工具调用经过本网关执行
注册检查 → 权限检查 → 参数校验 → 限额 → 结果大小检查 → 审计。

边界（REVISIONS-1.1 R6）:
- 工具调用计数、查询超时、结果大小限制在这里
- Token 预算不在这里（在 Agent Runtime 的 BudgetManager / Model Gateway）
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from traceops.runtime.budget import BudgetExhausted, BudgetManager


class ToolPermission(str, Enum):
    OBSERVABILITY_READ = "observability.read"
    REMEDIATION_WRITE = "remediation.write"


class ToolNotConfigured(RuntimeError):
    """内置工具尚未接数据源适配器（P0 阶段：先有契约，后接 Grafana MCP）。"""


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_schema: dict[str, Any]
    permission: ToolPermission
    timeout_seconds: float = 30.0
    max_result_bytes: int = 65536
    implementation: Callable[..., Any]
    audit_enabled: bool = True


class ToolGateway:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._audit: list[dict[str, Any]] = []

    # ---- registry ----
    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get_tool(self, name: str) -> ToolSpec:
        return self._tools[name]

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    # ---- invocation ----
    def invoke(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        budget: BudgetManager,
        allowed_permissions: list[ToolPermission] | None = None,
    ) -> Any:
        """执行一次受控工具调用。异常语义:
        - 未注册 / 权限不足 → PermissionError
        - 参数不合法 → jsonschema.ValidationError（原样抛出）
        - 预算耗尽 → BudgetExhausted
        """
        spec = self._tools.get(tool_name)
        if spec is None:
            self._record(tool_name, "UNKNOWN_TOOL", params, error="tool not registered")
            raise PermissionError(f"tool {tool_name!r} is not registered")

        allowed = allowed_permissions if allowed_permissions is not None else [ToolPermission.OBSERVABILITY_READ]
        if spec.permission not in allowed:
            self._record(tool_name, "DENIED_PERMISSION", params,
                         error=f"requires {spec.permission.value}")
            raise PermissionError(
                f"tool {tool_name!r} requires permission {spec.permission.value}; "
                f"allowed: {[p.value for p in allowed]}"
            )

        from jsonschema import ValidationError as _SchemaError

        try:
            Draft202012Validator(spec.input_schema).validate(params)
        except _SchemaError as exc:
            self._record(tool_name, "REJECTED_SCHEMA", params, error=exc.message[:500])
            raise

        try:
            budget.consume_tool_call()
        except BudgetExhausted as exc:
            self._record(tool_name, "BUDGET_EXHAUSTED", params, error=str(exc))
            raise

        started = time.monotonic()
        try:
            result = spec.implementation(**params)
        except Exception as exc:  # tool failure is audited, then propagated
            self._record(tool_name, "ERROR", params, error=repr(exc),
                         duration_ms=int((time.monotonic() - started) * 1000))
            raise

        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > spec.max_result_bytes:
            self._record(tool_name, "RESULT_TOO_LARGE", params,
                         error=f"{len(encoded)} bytes > {spec.max_result_bytes}",
                         duration_ms=int((time.monotonic() - started) * 1000))
            raise ValueError(
                f"tool {tool_name!r} result size {len(encoded)} bytes exceeds "
                f"limit {spec.max_result_bytes}"
            )

        self._record(tool_name, "OK", params,
                     duration_ms=int((time.monotonic() - started) * 1000))
        return result

    # ---- audit ----
    def _record(self, tool: str, outcome: str, params: dict[str, Any],
                *, error: str | None = None, duration_ms: int = 0) -> None:
        self._audit.append({
            "tool": tool,
            "outcome": outcome,
            "params": params,
            "error": error,
            "duration_ms": duration_ms,
            "timestamp": datetime.now(UTC),
        })

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)


# ---- built-in tool pool (全部只读) ----

def _unconfigured(name: str) -> Callable[..., Any]:
    def _raise(**_params: Any) -> Any:
        raise ToolNotConfigured(
            f"tool {name!r} has no data-source adapter configured yet "
            f"(P0: wire Grafana MCP in agent-runtime/tools/adapters.py)"
        )
    return _raise


_TIME_RANGE_PROPS = {
    "start": {"type": "string", "format": "date-time"},
    "end": {"type": "string", "format": "date-time"},
}


def register_builtin_tools(gw: ToolGateway, adapters: Any | None = None) -> None:
    """注册 P0 内置工具。adapters 未提供时实现抛 ToolNotConfigured。"""
    tools = [
        ToolSpec(
            name="query_metric_series",
            description="查询指定服务在时间窗口内的 Prometheus 指标序列",
            input_schema={
                "type": "object",
                "required": ["service", "metric", "start", "end"],
                "properties": {
                    "service": {"type": "string"},
                    "metric": {"type": "string"},
                    **_TIME_RANGE_PROPS,
                },
                "additionalProperties": False,
            },
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=getattr(adapters, "query_metric_series", None) or _unconfigured("query_metric_series"),
        ),
        ToolSpec(
            name="query_service_error_rate",
            description="查询指定服务在时间窗口内的 HTTP 错误率",
            input_schema={
                "type": "object",
                "required": ["service", "start", "end"],
                "properties": {
                    "service": {"type": "string"},
                    **_TIME_RANGE_PROPS,
                },
                "additionalProperties": False,
            },
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=getattr(adapters, "query_service_error_rate", None) or _unconfigured("query_service_error_rate"),
        ),
        ToolSpec(
            name="query_service_logs",
            description="查询指定服务在时间窗口内匹配过滤条件的日志",
            input_schema={
                "type": "object",
                "required": ["service", "start", "end"],
                "properties": {
                    "service": {"type": "string"},
                    "filter": {"type": "string"},
                    **_TIME_RANGE_PROPS,
                },
                "additionalProperties": False,
            },
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=getattr(adapters, "query_service_logs", None) or _unconfigured("query_service_logs"),
        ),
        ToolSpec(
            name="query_service_traces",
            description="查询指定服务在时间窗口内的错误/慢 Trace 摘要",
            input_schema={
                "type": "object",
                "required": ["service", "start", "end"],
                "properties": {
                    "service": {"type": "string"},
                    "min_duration_ms": {"type": "integer", "minimum": 0},
                    **_TIME_RANGE_PROPS,
                },
                "additionalProperties": False,
            },
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=getattr(adapters, "query_service_traces", None) or _unconfigured("query_service_traces"),
        ),
    ]
    for tool in tools:
        gw.register(tool)
