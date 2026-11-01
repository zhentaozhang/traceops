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

    #: 这条序列**测的是什么**。它不是装饰：故障机制的分类要由"哪个测量动了"决定。
    #:
    #: 三种取值对应三种真实指标：
    #: * `error`   —— 请求错误率 / 错误计数（`service_errors_total`）
    #: * `latency` —— 响应时延分位（`service_latency_seconds`），
    #:   异常含义是"变慢"而不是"出错"
    #: * `resource` —— CPU / 内存 / 磁盘等资源量（`service_resource_usage`）
    #:
    #: 把三者混成一条"错误率"，等于把"这个服务慢"和"这个服务错"说成同一件事。
    #: 实测代价：RCAEval RE1-OB 上 24 个 delay 案例（真因机制是延迟劣化）被我们
    #: 一律答成 availability_degradation，FT@1 因此只有 0.79。
    metric_kind: Literal["error", "latency", "resource"] = "error"
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
        """本场景**应该**被引用到的证据类型（evidence_recall 的分母）。

        必须同时看顶层字段与 `per_service`：多服务场景把信号写在 per_service 里
        （F-005/F-006/F-008/F-009 都是这种形状），只看顶层会让集合为空，
        于是 evidence_recall 的分母为 0、恒返回 0.0 —— 一个"分母算错"造成的
        伪失败，而不是"没找到证据"。
        """
        types: set[EvidenceType] = set()
        if self.error_rate_series:
            types.add(EvidenceType.METRIC)
        if self.error_logs:
            types.add(EvidenceType.LOG)
        if self.slow_spans:
            types.add(EvidenceType.TRACE)
        if self.config_change is not None:
            types.add(EvidenceType.EVENT)
        # 多服务：各服务信号类型的并集
        for service_signals in (self.per_service or {}).values():
            if service_signals.error_rate_series:
                types.add(EvidenceType.METRIC)
            if service_signals.error_logs:
                types.add(EvidenceType.LOG)
            if service_signals.slow_spans:
                types.add(EvidenceType.TRACE)
        return types


class GroundTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    component: str | None = None
    fault_type: str
    expected_diagnosis: Literal["SUPPORTED", "REFUTED", "INCONCLUSIVE"] = "SUPPORTED"
    propagation_chain: list[str] = Field(default_factory=list)
    # ---- rubric（借鉴 aurora-lab scenarios/*/scenario.yaml）----
    # 报告**必须**点到的东西。不止一个根因服务时，用来防「只找到第一个就宣布收工」。
    must_identify: list[str] = Field(
        default_factory=list,
        description="报告中必须出现的实体（服务名/组件名）；缺一个即记一次 missing_identification",
    )
    # **看起来合理但实际错误**的答案，被系统说出口就算一次错误断言。
    # 这是「规则自证」的解药：只有列出错误答案，才测得出「方向错但听起来专业」。
    wrong_answers: list[str] = Field(
        default_factory=list,
        description="明确错误的根因表述（如 'memory leak'）；出现在报告中即计为一次错误断言",
    )
    # 除 service 之外的**其他**独立根因。多故障场景用：
    # 只有一个 `service` 字段时，"第二个故障被漏报"无法被指标发现。
    additional_root_causes: list[str] = Field(
        default_factory=list,
        description="与 service 互不相干、同时发生的其他根因服务；报告中应各自成为 SUPPORTED 候选",
    )


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    name: str
    description: str = ""
    service_hint: str
    #: 告警原文（可选）。
    #:
    #: 为什么需要：告警 summary 常常含**自然语言线索**（"traced to cache tier:
    #: zulu-cache responses degraded"）。规则不读它，所以这类线索对纯规则路径
    #: 完全不可见 —— 而人能读、模型也能读。
    #:
    #: 把它作为场景的一等字段（而不是塞进 signals），是为了让
    #: 「规则读不到的信息」成为可测的差异来源。
    alert_summary: str | None = None
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
    #: **没有拓扑时**要查哪些服务。与 `topology_edges` 的区别是它不带方向：
    #: 真实数据集（RCAEval）不发布调用图，而我们曾经编过一个"以告警服务为中心的
    #: 星形"，等于宣称"告警的服务依赖所有别的服务"——症状判定于是把真因降级。
    #: 服务清单只决定取证范围，让"排序"这件事能发生，而不替我们猜方向。
    scope_services: list[str] = Field(
        default_factory=list,
        description="无拓扑时的取证范围（服务名清单，不含方向）",
    )

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


def load_scenarios(directory: str | Path, *, pattern: str = "*.json") -> list[Scenario]:
    """读一个目录下的全部场景文件。

    默认 `*.json` 而不是 `F-*.json`：导入的真实案例（RCAEval）文件名是
    `rcaeval-*`，写死前缀会让"换个数据源"这件事静默变成"没有场景"——
    而那正是最容易被误读成"数据集空的"的症状。坏文件直接抛，不跳过。
    """
    paths = sorted(p for p in Path(directory).glob(pattern))
    return [load_scenario(p) for p in paths]
