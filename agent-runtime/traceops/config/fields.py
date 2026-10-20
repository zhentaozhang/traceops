"""策略旋钮的字段声明 + 校验 + 差异。

一份声明服务三个消费者：

1. **校验**（`validate_knobs`）：值域与类型在进入系统之前就被拒绝，
   而不是在"阈值永远不触发"的沉默里被发现。
2. **取值**（`current_knobs`）：`strategy_components()` 从这里读，
   于是"哪个旋钮参与指纹"只有一处需要维护。
3. **差异**（`diff_knobs`）：两份配置的差别以点分路径列出，
   可以直接进日志/报告，而不是"两个 dict 肉眼 diff"。

`default` 一律**从实现里现读**，不许手抄 —— 手抄的默认值一定会漂移。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

#: 支持的字段类型。故意只有三种：旋钮要么是数、要么是开关，没有自由文本
#: （自由文本旋钮无法校验，也就无法防止"看起来配了、其实没生效"）。
KIND_NUMBER = "number"
KIND_INTEGER = "integer"
KIND_BOOLEAN = "boolean"

_KINDS = (KIND_NUMBER, KIND_INTEGER, KIND_BOOLEAN)


class FieldError(ValueError):
    """旋钮取值非法。消息里必须带路径、期望、实际值。"""


@dataclass(frozen=True)
class FieldSpec:
    """一个旋钮的完整声明。"""

    path: str
    kind: str
    default: Any
    description: str
    minimum: float | None = None
    maximum: float | None = None
    #: 读当前实现里的值。`None` 表示默认值就是权威源（如预算默认值）。
    read: Callable[[], Any] | None = None

    def __post_init__(self) -> None:
        if not self.path or "." not in self.path:
            raise FieldError(f"field path must be dotted: {self.path!r}")
        if self.kind not in _KINDS:
            raise FieldError(f"field {self.path}: unknown kind {self.kind!r}")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum > self.maximum:
                raise FieldError(
                    f"field {self.path}: minimum {self.minimum} > maximum {self.maximum}")

    # ---- 校验 ----

    def coerce(self, value: Any) -> Any:
        """校验并返回规范化后的值。非法即抛，不做"尽力而为"的纠正。"""
        if self.kind == KIND_BOOLEAN:
            if isinstance(value, bool):
                return value
            raise FieldError(
                f"field {self.path}: expected boolean, got {value!r} ({type(value).__name__})")
        if isinstance(value, bool):
            # True/False 是 int 的子类，不拦住的话 `max_tool_calls=True` 会被
            # 当成 1 —— 静默地把预算掐到 1 次是最坏的一类"能跑但不工作"。
            raise FieldError(f"field {self.path}: expected number, got boolean {value!r}")
        if not isinstance(value, (int, float)):
            raise FieldError(
                f"field {self.path}: expected number, got {value!r} ({type(value).__name__})")
        if self.kind == KIND_INTEGER:
            if isinstance(value, float) and not value.is_integer():
                raise FieldError(
                    f"field {self.path}: expected integer, got fractional {value!r}")
            value = int(value)
        if self.minimum is not None and value < self.minimum:
            raise FieldError(
                f"field {self.path}: {value!r} is below the minimum {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise FieldError(
                f"field {self.path}: {value!r} is above the maximum {self.maximum}")
        return value

    def current(self) -> Any:
        """实现里的当前取值（缺省等于 default）。"""
        return self.read() if self.read is not None else self.default

    def describe(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "default": self.default,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "description": self.description,
        }


def _read_error_rate() -> float:
    from traceops.orchestration import detectors

    return detectors.ERROR_RATE_THRESHOLD


def _read_slow_span() -> float:
    from traceops.orchestration import detectors

    return detectors.SLOW_SPAN_THRESHOLD_SECONDS


def _read_weight(name: str) -> Callable[[], int]:
    def read() -> int:
        from traceops.rca import ranking

        return getattr(ranking, name)

    return read


def _read_budget(name: str) -> Callable[[], Any]:
    def read() -> Any:
        from traceops.runtime import budget

        return getattr(budget, name)

    return read


_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        path="thresholds.error_rate", kind=KIND_NUMBER, default=0.05,
        description="error rate above which a metric counts as an anomaly (0..1)",
        minimum=0.0, maximum=1.0, read=_read_error_rate,
    ),
    FieldSpec(
        path="thresholds.slow_span_seconds", kind=KIND_NUMBER, default=0.5,
        description="span duration above which a trace counts as slow",
        minimum=0.0, maximum=600.0, read=_read_slow_span,
    ),
    FieldSpec(
        path="ranking.demote", kind=KIND_INTEGER, default=-1,
        description="weight for a demoted hypothesis",
        minimum=-10, maximum=0, read=_read_weight("_W_DEMOTE"),
    ),
    FieldSpec(
        path="ranking.anomaly_before", kind=KIND_INTEGER, default=2,
        description="weight when the hypothesis' service showed the anomaly first",
        minimum=-10, maximum=10, read=_read_weight("_W_ANOMALY_BEFORE"),
    ),
    FieldSpec(
        path="ranking.trace_anomaly", kind=KIND_INTEGER, default=2,
        description="weight for a trace-level anomaly in the same window",
        minimum=-10, maximum=10, read=_read_weight("_W_TRACE_ANOMALY"),
    ),
    FieldSpec(
        path="ranking.config_change", kind=KIND_INTEGER, default=3,
        description="weight when a config change preceded the fault",
        minimum=-10, maximum=10, read=_read_weight("_W_CONFIG_CHANGE"),
    ),
    FieldSpec(
        path="ranking.contradiction", kind=KIND_INTEGER, default=-3,
        description="weight for a hypothesis contradicted by evidence",
        minimum=-10, maximum=10, read=_read_weight("_W_CONTRADICTION"),
    ),
    FieldSpec(
        path="ranking.component_specific", kind=KIND_INTEGER, default=1,
        description="weight for naming a specific component instead of a service",
        minimum=-10, maximum=10, read=_read_weight("_W_COMPONENT_SPECIFIC"),
    ),
    FieldSpec(
        path="budget.max_tool_calls", kind=KIND_INTEGER, default=50,
        description="hard cap on datasource queries per investigation",
        minimum=1, maximum=1000, read=_read_budget("DEFAULT_MAX_TOOL_CALLS"),
    ),
    FieldSpec(
        path="budget.max_rounds", kind=KIND_INTEGER, default=10,
        description="hard cap on model rounds per investigation",
        minimum=1, maximum=100, read=_read_budget("DEFAULT_MAX_ROUNDS"),
    ),
    FieldSpec(
        path="budget.max_tokens", kind=KIND_INTEGER, default=100_000,
        description="hard cap on model tokens per investigation",
        minimum=1000, maximum=10_000_000, read=_read_budget("DEFAULT_MAX_TOKENS"),
    ),
    FieldSpec(
        path="budget.deadline_seconds", kind=KIND_NUMBER, default=900,
        description="wall-clock cap per investigation (seconds)",
        minimum=1, maximum=86_400, read=_read_budget("DEFAULT_DEADLINE_SECONDS"),
    ),
)

#: 全部旋钮。键是点分路径。
STRATEGY_FIELDS: dict[str, FieldSpec] = {spec.path: spec for spec in _FIELDS}


def current_knobs() -> dict[str, Any]:
    """实现里的当前取值（点分路径 → 值）。"""
    return {path: spec.current() for path, spec in STRATEGY_FIELDS.items()}


def validate_knobs(overrides: dict[str, Any]) -> dict[str, Any]:
    """校验一组覆盖值，返回合并了默认值的完整配置。

    未知路径一律拒绝：静默忽略一个拼错的键，等于让配置看起来生效了、
    实际什么也没改。
    """
    unknown = sorted(set(overrides) - set(STRATEGY_FIELDS))
    if unknown:
        raise FieldError(
            "unknown configuration field(s): " + ", ".join(unknown)
            + " (known: " + ", ".join(sorted(STRATEGY_FIELDS)) + ")")
    resolved = current_knobs()
    for path, value in overrides.items():
        resolved[path] = STRATEGY_FIELDS[path].coerce(value)
    return resolved


def diff_knobs(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """两份配置的差异，点分路径排序返回。

    只比较两边都出现的键（缺失的键由调用方用 `validate_knobs` 补齐），
    这样"历史配置里没有这个旋钮"（跨版本）不会被误报成"值变了"。
    """
    changed = [
        path for path in sorted(set(before) & set(after))
        if before[path] != after[path]
    ]
    return changed


def describe_fields() -> list[dict[str, Any]]:
    """给文档/接口用的字段说明列表（顺序即声明顺序）。"""
    return [spec.describe() for spec in _FIELDS]
