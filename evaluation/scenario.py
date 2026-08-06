"""场景定义：一次可复现的故障实验（注入什么 + ground truth 是什么）。

P0 场景全部是**合成**的（信号由 signals 声明式生成），因为 P0 还没有
真实靶场可自动注入故障（见 技术验证记录 S4）。合成的价值是：
每个场景的 ground truth 是确定的，指标因此可复现；局限是它无法覆盖
真实数据的噪声与缺失，这一点必须写进 EVALUATION.md，不能当作真实准确率。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from traceops.domain.models import EvidenceType


class SlowSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    duration_ms: float


class ConfigChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changed_at: datetime
    component: str
    summary: str = ""


class ServiceSignals(BaseModel):
    """某个服务自身的信号（多服务场景用）。"""

    model_config = ConfigDict(extra="forbid")

    error_rate_series: list[float] = Field(default_factory=list)
    error_logs: list[str] = Field(default_factory=list)
    slow_spans: list[SlowSpan] = Field(default_factory=list)
    # 相对 fault_start 的偏移（毫秒），用于表达"症状晚于根因"
    span_offset_ms: int = 0

    def has_any(self) -> bool:
        return bool(self.error_rate_series or self.error_logs or self.slow_spans)


class Signals(BaseModel):
    """声明式信号：harness 据此生成 Prometheus/Loki/Tempo 形状的 payload。"""

    model_config = ConfigDict(extra="forbid")

    error_rate_series: list[float] = Field(default_factory=list)
    error_logs: list[str] = Field(default_factory=list)
    slow_spans: list[SlowSpan] = Field(default_factory=list)
    config_change: ConfigChange | None = None
    # 多服务信号：key=服务名。与上面的单服务字段互斥（不要同时用，避免歧义）
    per_service: dict[str, ServiceSignals] = Field(default_factory=dict)

    def expected_evidence_types(self) -> set[EvidenceType]:
        types: set[EvidenceType] = set()
        if self.error_rate_series:
            types.add(EvidenceType.METRIC)
        if self.error_logs:
            types.add(EvidenceType.LOG)
        if self.slow_spans:
            types.add(EvidenceType.TRACE)
        if self.config_change is not None:
            types.add(EvidenceType.EVENT)
        return types


class GroundTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    component: str | None = None
    fault_type: str
    expected_diagnosis: Literal["SUPPORTED", "REFUTED", "INCONCLUSIVE"] = "SUPPORTED"
    propagation_chain: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    name: str
    description: str = ""
    service_hint: str
    fault_start: datetime
    window_start: datetime
    window_end: datetime
    signals: Signals = Field(default_factory=Signals)
    ground_truth: GroundTruth
    topology_edges: list[list[str]] = Field(
        default_factory=list,
        description="依赖边列表 [[caller, callee], ...]；为空则不启用拓扑扩展",
    )
    scope_depth: int = 1

    @model_validator(mode="after")
    def _validate_times(self) -> Scenario:
        for name, value in (
            ("fault_start", self.fault_start),
            ("window_start", self.window_start),
            ("window_end", self.window_end),
        ):
            if value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware (UTC)")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be before window_end")
        if not (self.window_start <= self.fault_start <= self.window_end):
            raise ValueError("fault_start must fall inside the investigation window")
        if self.signals.per_service and (self.signals.error_rate_series or self.signals.error_logs
                                         or self.signals.slow_spans):
            raise ValueError("signals.per_service cannot be combined with single-service signal fields")
        for edge in self.topology_edges:
            if len(edge) != 2:
                raise ValueError("topology_edges entries must be [caller, callee] pairs")

        changed_at = self.signals.config_change.changed_at if self.signals.config_change else None
        if changed_at is not None and not (self.window_start <= changed_at <= self.fault_start):
            # Evidence 不变量要求 observed_at 落在调查窗口内；配置变更应在故障前、窗口内
            raise ValueError("config_change.changed_at must fall inside the window and before fault_start")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_scenario(path: str | Path) -> Scenario:
    return Scenario.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_scenarios(directory: str | Path) -> list[Scenario]:
    paths = sorted(p for p in Path(directory).glob("F-*.json"))
    return [load_scenario(p) for p in paths]
