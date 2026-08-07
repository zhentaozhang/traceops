"""查询模板注册表（蓝图 §6.1）。

## 为什么必须模板化

如果不模板化，Agent 会直接生成 PromQL / LogQL。这带来三个问题：
1. 查询没有边界（`{service=~".*"}` 能扫全库）
2. 查询语句无法审计（同一个调查里两次跑的语句不同，无法复现）
3. 提示注入可以让日志内容诱导 Agent 生成危险查询

模板化后 Agent 只能**选模板 + 填参数**，参数再经 schema 校验。

## 模板必须声明

- `template_id`：稳定的标识，写进 Evidence.query_template_id 供追溯
- `evidence_type`：产出哪类证据
- `parameter_schema`：允许的参数（服务、时间窗、过滤词）
- `render`：把参数渲染成数据源原生查询（PromQL / LogQL / TraceQL）

`render` 是纯函数，没有副作用，便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError

from traceops.domain.models import EvidenceType


class QueryTemplateError(ValueError):
    """模板不存在、参数非法、或渲染结果越界。"""


@dataclass(frozen=True)
class QueryTemplate:
    template_id: str
    description: str
    evidence_type: EvidenceType
    parameter_schema: dict[str, Any]
    render: Callable[[dict[str, Any]], str]
    # 该模板最多允许查询的时间窗（秒）。防止"查一年"这种成本炸弹。
    max_window_seconds: int = 3600

    def build(self, parameters: dict[str, Any]) -> str:
        try:
            Draft202012Validator(self.parameter_schema).validate(parameters)
        except SchemaValidationError as exc:
            raise QueryTemplateError(
                f"template {self.template_id!r}: invalid parameters: {exc.message}"
            ) from exc
        return self.render(parameters)


class TemplateRegistry:
    def __init__(self) -> None:
        self._templates: dict[str, QueryTemplate] = {}

    def register(self, template: QueryTemplate) -> None:
        if template.template_id in self._templates:
            raise QueryTemplateError(f"template {template.template_id!r} already registered")
        self._templates[template.template_id] = template

    def get(self, template_id: str) -> QueryTemplate:
        try:
            return self._templates[template_id]
        except KeyError as exc:
            raise QueryTemplateError(f"template {template_id!r} is not registered") from exc

    def list_ids(self) -> list[str]:
        return sorted(self._templates)

    def build(self, template_id: str, parameters: dict[str, Any]) -> str:
        return self.get(template_id).build(parameters)


# ---------------- 内置模板 ----------------

_TIME = {
    "start": {"type": "string", "format": "date-time"},
    "end": {"type": "string", "format": "date-time"},
}
_SERVICE = {"type": "string", "minLength": 1, "maxLength": 128,
            # 服务名来自拓扑/事件，不允许正则元字符，避免注入到选择器里
            "pattern": "^[a-zA-Z0-9][a-zA-Z0-9._-]*$"}

_FILTER = {"type": "string", "maxLength": 256, "pattern": r'^[^"\\]*$',
           "description": "日志过滤词；禁止引号与反斜杠（防 LogQL 注入）"}


def _metric_selector(params: dict[str, Any]) -> str:
    return f'{params["metric"]}{{service="{params["service"]}"}}'


def register_builtin_templates(registry: TemplateRegistry) -> None:
    registry.register(QueryTemplate(
        template_id="qt-error-rate-v1",
        description="指定服务的 5xx 错误占比（PromQL）",
        evidence_type=EvidenceType.METRIC,
        parameter_schema={
            "type": "object", "additionalProperties": False,
            "required": ["service", "start", "end"],
            "properties": {"service": _SERVICE, **_TIME},
        },
        render=lambda p: (
            f'sum(rate(http_requests_total{{service="{p["service"]}",status=~"5.."}}[1m]))'
            f' / clamp_min(sum(rate(http_requests_total{{service="{p["service"]}"}}[1m])), 0.000001)'
        ),
    ))
    registry.register(QueryTemplate(
        template_id="qt-metric-series-v1",
        description="指定指标在时间窗内的序列（PromQL selector）",
        evidence_type=EvidenceType.METRIC,
        parameter_schema={
            "type": "object", "additionalProperties": False,
            "required": ["service", "metric", "start", "end"],
            "properties": {
                "service": _SERVICE,
                "metric": {"type": "string", "minLength": 1, "maxLength": 128,
                           "pattern": "^[a-zA-Z_][a-zA-Z0-9_]*$"},
                **_TIME,
            },
        },
        render=_metric_selector,
    ))
    registry.register(QueryTemplate(
        template_id="qt-error-logs-v1",
        description="指定服务的错误日志（LogQL stream selector + 行过滤）",
        evidence_type=EvidenceType.LOG,
        parameter_schema={
            "type": "object", "additionalProperties": False,
            "required": ["service", "start", "end"],
            "properties": {"service": _SERVICE, "filter": _FILTER, **_TIME},
        },
        render=lambda p: (
            f'{{service="{p["service"]}",level="error"}}'
            + (f' |= "{p["filter"]}"' if p.get("filter") else "")
        ),
    ))
    registry.register(QueryTemplate(
        template_id="qt-slow-traces-v1",
        description="指定服务的慢 Trace（Tempo search 条件）",
        evidence_type=EvidenceType.TRACE,
        parameter_schema={
            "type": "object", "additionalProperties": False,
            "required": ["service", "start", "end"],
            "properties": {
                "service": _SERVICE,
                "min_duration_ms": {"type": "integer", "minimum": 1, "maximum": 60000},
                **_TIME,
            },
        },
        render=lambda p: (
            f'{{service.name="{p["service"]}"}}'
            + (f" && duration > {p['min_duration_ms']}ms" if p.get("min_duration_ms") else "")
        ),
    ))


def default_registry() -> TemplateRegistry:
    registry = TemplateRegistry()
    register_builtin_templates(registry)
    return registry
