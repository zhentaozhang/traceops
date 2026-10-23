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

## 为什么模板里的名字必须可声明

模板把遥测的**约定**写死过：`http_requests_total`、标签 `service`、状态标签
`status=~"5.."`、Loki 的 `level="error"`、Tempo 的 `service.name`。这些是
某一套采集配置的产物，不是事实。别人的集群里叫 `http_server_requests_seconds_count`
或标签叫 `app` 时，所有查询都会返回空 —— 而报告只会说"查了但没有"，把
"约定不匹配"伪装成"没有异常"。

所以约定提到 `TelemetryConventions` 里，由部署方声明（见
`load_conventions`），模板只负责用这些名字拼查询。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
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

# ---------------- 遥测约定（可声明） ----------------

#: 指标名允许的字符（Prometheus 指标名：`[a-zA-Z_:][a-zA-Z0-9_:]*`）。
_METRIC_NAME_RE = r"^[a-zA-Z_:][a-zA-Z0-9_:]*$"
#: 标签名允许的字符。比 Prometheus 更严（不接受 UTF-8），因为标签名要被
#: 直接拼进选择器，宽字符集合只会让"能拼进去"变成"能被利用"。
_LABEL_NAME_RE = r"^[a-zA-Z_][a-zA-Z0-9_]*$"
#: Tempo 的 span 属性键允许点号（`service.name` 是默认约定），所以单独放宽
#: 这一个字段；Prometheus 的标签名不允许点号，两边不能共用一个正则。
_SPAN_ATTR_RE = r"^[a-zA-Z_][a-zA-Z0-9_.]*$"


@dataclass(frozen=True)
class TelemetryConventions:
    """部署方的遥测约定。

    默认值 = 仓库自带演示栈（`demo-environment/observability`）的约定。
    换一套约定不等于"另一种查询"，而是"另一套事实来源"：名字对不上时
    查询返回空，而空结果会被下游判成 `no_data`（阴性观测），不是错误。
    """

    #: 请求计数指标名（错误率的分子分母都来自它）。
    requests_total: str = "http_requests_total"
    #: 承载服务名的标签。
    service_label: str = "service"
    #: 承载 HTTP 状态码的标签。
    status_label: str = "status"
    #: 视为"服务端错误"的状态码正则。
    error_status_regex: str = "5.."
    #: 日志流的服务标签。
    log_service_label: str = "service"
    #: 日志流的等级标签。
    log_level_label: str = "level"
    #: 日志流里"错误"等级的取值。
    log_error_value: str = "error"
    #: Trace 上承载服务名的标签（Tempo 用带点的名字）。
    trace_service_label: str = "service.name"

    def __post_init__(self) -> None:
        if not re.fullmatch(_METRIC_NAME_RE, self.requests_total):
            raise QueryTemplateError(
                f"requests_total must be a Prometheus metric name, got {self.requests_total!r}"
            )
        for field_name in ("service_label", "status_label", "log_service_label",
                           "log_level_label"):
            value = getattr(self, field_name)
            if not re.fullmatch(_LABEL_NAME_RE, value):
                raise QueryTemplateError(
                    f"{field_name} must be a label name, got {value!r}"
                )
        if not re.fullmatch(_SPAN_ATTR_RE, self.trace_service_label):
            raise QueryTemplateError(
                "trace_service_label must be a span attribute key, got "
                f"{self.trace_service_label!r}"
            )
        try:
            re.compile(self.error_status_regex)
        except re.error as exc:
            raise QueryTemplateError(
                f"error_status_regex is not a valid regular expression: {exc}"
            ) from exc
        # LogQL 的等级是比较，放行任何非空字符串；空串会让选择器变成
        # `level=""`，那不是"所有等级"，是"等级为空"。
        if not self.log_error_value.strip():
            raise QueryTemplateError("log_error_value must not be blank")

    def to_dict(self) -> dict[str, str]:
        return {
            "requests_total": self.requests_total,
            "service_label": self.service_label,
            "status_label": self.status_label,
            "error_status_regex": self.error_status_regex,
            "log_service_label": self.log_service_label,
            "log_level_label": self.log_level_label,
            "log_error_value": self.log_error_value,
            "trace_service_label": self.trace_service_label,
        }


def load_conventions(path: str | Path) -> TelemetryConventions:
    """从 JSON 文件读约定。未知字段直接拒绝（拼错一个键就会静默沿用默认值）。"""
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise QueryTemplateError(f"conventions file must hold an object, got {type(raw).__name__}")
    known = set(TelemetryConventions.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise QueryTemplateError(
            f"unknown convention field(s): {', '.join(unknown)}; known: {', '.join(sorted(known))}"
        )
    for key, value in raw.items():
        if not isinstance(value, str):
            raise QueryTemplateError(f"convention {key!r} must be a string, got {type(value).__name__}")
    return TelemetryConventions(**raw)


#: 环境变量：指向约定 JSON 文件。设了就用它，没设就用演示栈约定。
CONVENTIONS_ENV = "TRACEOPS_TELEMETRY_CONVENTIONS"


@lru_cache(maxsize=4)
def _conventions_from_env(path: str) -> TelemetryConventions:
    return load_conventions(path)


def active_conventions() -> TelemetryConventions:
    """当前生效的约定：环境变量指向的文件优先，否则默认值。"""
    path = os.environ.get(CONVENTIONS_ENV)
    return _conventions_from_env(path) if path else TelemetryConventions()


_FILTER = {"type": "string", "maxLength": 256, "pattern": r'^[^"\\]*$',
           "description": "日志过滤词；禁止引号与反斜杠（防 LogQL 注入）"}


def render_error_rate(service: str, conventions: TelemetryConventions) -> str:
    """错误率查询。判别回路也用它 —— 同一件事只写一次。"""
    sel_err = (
        f'{conventions.requests_total}{{{conventions.service_label}="{service}",'
        f'{conventions.status_label}=~"{conventions.error_status_regex}"}}'
    )
    sel_all = f'{conventions.requests_total}{{{conventions.service_label}="{service}"}}'
    return (
        f"sum(rate({sel_err}[1m]))"
        f" / clamp_min(sum(rate({sel_all}[1m])), 0.000001)"
    )


def render_logs(
    service: str, conventions: TelemetryConventions, *, filter_text: str | None = None
) -> str:
    selector = (
        f'{{{conventions.log_service_label}="{service}",'
        f'{conventions.log_level_label}="{conventions.log_error_value}"}}'
    )
    return selector + (f' |= "{filter_text}"' if filter_text else "")


def render_traces(
    service: str, conventions: TelemetryConventions, *, min_duration_ms: int | None = None
) -> str:
    selector = f'{{{conventions.trace_service_label}="{service}"}}'
    return selector + (f" && duration > {min_duration_ms}ms" if min_duration_ms else "")


def _metric_selector(params: dict[str, Any]) -> str:
    service_label = params.get("__service_label", "service")
    return f'{params["metric"]}{{{service_label}="{params["service"]}"}}'


def register_builtin_templates(
    registry: TemplateRegistry, conventions: TelemetryConventions | None = None
) -> None:
    conventions = conventions or TelemetryConventions()
    registry.register(QueryTemplate(
        template_id="qt-error-rate-v1",
        description="指定服务的 5xx 错误占比（PromQL）",
        evidence_type=EvidenceType.METRIC,
        parameter_schema={
            "type": "object", "additionalProperties": False,
            "required": ["service", "start", "end"],
            "properties": {"service": _SERVICE, **_TIME},
        },
        render=lambda p: render_error_rate(p["service"], conventions),
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
        render=lambda p: _metric_selector({**p, "__service_label": conventions.service_label}),
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
        render=lambda p: render_logs(
            p["service"], conventions, filter_text=p.get("filter")
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
        render=lambda p: render_traces(
            p["service"], conventions, min_duration_ms=p.get("min_duration_ms")
        ),
    ))


def default_registry(conventions: TelemetryConventions | None = None) -> TemplateRegistry:
    registry = TemplateRegistry()
    register_builtin_templates(registry, conventions or active_conventions())
    return registry
