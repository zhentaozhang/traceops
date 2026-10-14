"""TraceOps P0 domain models.

契约来源:
- 蓝图 §5.3  Evidence 字段定义
- 修订记录 R2     HypothesisEvidence(证据↔假设) 与 EvidenceRelation(证据↔证据) 分离
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4()}"


class EvidenceType(str, Enum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    EVENT = "event"
    # 持续剖析（Pyroscope）：CPU/内存热点。
    #
    # 为什么是独立类型而不是塞进 metric：它回答的是**完全不同的问题**。
    #   metric -> 慢了多少（错误率/分位）
    #   trace  -> 哪个调用慢（跨度）
    #   profile-> 慢在哪个函数/哪一行
    # 没有 profile，`latency_degradation` 只能停在"redis.command 慢 2.1s"，
    # 无法回答"慢在解析还是锁竞争" —— 而答案就在 profile 里。
    PROFILE = "profile"


class RelationType(str, Enum):
    """P0 只支持证据→假设关系。证据↔证据关系(PRECEDES/SAME_TRACE/...)延后 P1 (R2)。"""

    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"


class EvidenceRelationType(str, Enum):
    """证据 ↔ 证据的关系（P1，R2）。

    与 `RelationType`（证据 → 假设的 SUPPORTS/CONTRADICTS）是**两个不同维度**：
    前者描述事实之间怎么相连，后者描述事实支持哪个结论。
    混在一起会导致"想查支持某假设的证据"时把证据间关系也捞出来。
    """

    PRECEDES = "PRECEDES"          # A 在时间上早于 B（时间先后，不是因果证明）
    SAME_TRACE = "SAME_TRACE"      # 两条证据来自同一条调用链
    CORRELATED_WITH = "CORRELATED_WITH"  # 统计相关，需 strength 表达强弱
    AFFECTS = "AFFECTS"            # A 影响了某个服务


class CreatedBy(str, Enum):
    RULE = "rule"
    MODEL = "model"
    HUMAN = "human"


class HypothesisStatus(str, Enum):
    PROPOSED = "proposed"
    INVESTIGATING = "investigating"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


class Evidence(BaseModel):
    """从可信观测数据源获取的、确定时间范围内观测到的、可重新追溯的事实记录。

    不是 LLM 的推理过程，不是 Prompt 文本，不是 Agent 编造的结论。
    """

    model_config = ConfigDict(validate_assignment=True)

    # 原始 payload 由 Normalizer 附加，仅供 Store 落快照使用；不参与序列化/比较
    # （生产版写对象存储，这里以私有属性随对象传递）
    _raw_payload: dict[str, Any] | None = PrivateAttr(default=None)

    id: str = Field(default_factory=lambda: _new_id("e"))
    incident_id: str
    investigation_run_id: str
    evidence_type: EvidenceType
    source_id: str
    service_id: str | None = None
    observed_at: datetime
    window_start: datetime | None = None
    window_end: datetime | None = None
    collected_at: datetime = Field(default_factory=_utcnow)
    query_template_id: str
    query_params: dict[str, Any]
    rendered_query: str | None = Field(
        default=None,
        description=(
            "模板渲染后实际发给数据源的查询语句（PromQL/LogQL/TraceQL）。"
            "审计必需：同一模板+参数在不同模板版本下可能生成不同语句，"
            "只存 template_id 追溯不到真正执行了什么。"
            "变更事件类证据没有查询语句，因此可为 None。"
        ),
    )
    normalized_data: dict[str, Any]
    raw_snapshot_ref: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    source_link: str | None = None
    checksum: str = Field(default="", description="由 Evidence Store 在持久化快照时计算，不由调用方提供")
    schema_version: str = Field(default="1.0")

    @model_validator(mode="after")
    def _check_times(self) -> "Evidence":
        if self.collected_at < self.observed_at:
            raise ValueError(
                f"collected_at ({self.collected_at}) must not be earlier than "
                f"observed_at ({self.observed_at})"
            )
        if self.window_start is not None and self.observed_at < self.window_start:
            raise ValueError(
                f"observed_at ({self.observed_at}) before window_start ({self.window_start})"
            )
        if self.window_end is not None and self.observed_at > self.window_end:
            raise ValueError(
                f"observed_at ({self.observed_at}) after window_end ({self.window_end})"
            )
        return self


class EvidenceRelation(BaseModel):
    """证据之间的事实关系（P1）。

    只表达"事实之间怎么相连"，不表达因果。`PRECEDES` 仅仅意味着时间更早，
    真正的因果必须由诊断层基于证据论证，不能靠一张关系表宣布。
    """

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    id: str = Field(default_factory=lambda: _new_id("rel"))
    source_evidence_id: str
    target_evidence_id: str
    relation_type: EvidenceRelationType
    created_by: CreatedBy = CreatedBy.RULE
    # 供审计的一句话解释（人或模型产生的都要能说清依据）
    explanation: str | None = None
    # 仅 PRECEDES：source 比 target 早多少秒
    time_delta_seconds: float | None = None
    # 仅 SAME_TRACE：共享的 trace_id，没有它这条关系无法验证
    trace_id: str | None = None
    # 仅 CORRELATED_WITH：相关强度（0..1），不是概率
    strength: float | None = None
    # 仅 AFFECTS：被影响的服务
    affected_service: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "EvidenceRelation":
        if self.source_evidence_id == self.target_evidence_id:
            raise ValueError("evidence relation cannot point to itself")

        if self.relation_type is EvidenceRelationType.SAME_TRACE and not self.trace_id:
            raise ValueError("SAME_TRACE relation requires trace_id")

        if self.relation_type is EvidenceRelationType.PRECEDES:
            if self.time_delta_seconds is not None and self.time_delta_seconds < 0:
                raise ValueError("PRECEDES time_delta_seconds must be non-negative")

        if self.relation_type is EvidenceRelationType.CORRELATED_WITH:
            if self.strength is not None and not (0.0 <= self.strength <= 1.0):
                raise ValueError("CORRELATED_WITH strength must be within [0, 1]")

        if self.relation_type is EvidenceRelationType.AFFECTS and not self.affected_service:
            raise ValueError("AFFECTS relation requires affected_service")

        return self


class HypothesisEvidence(BaseModel):
    """一条证据与一个根因假设之间的支持/反驳关系 (P0, R2)。"""

    model_config = ConfigDict(validate_assignment=True)

    hypothesis_id: str
    evidence_id: str
    relation_type: RelationType
    reason: str | None = None
    created_by: CreatedBy
    created_at: datetime = Field(default_factory=_utcnow)


class RootCauseHypothesis(BaseModel):
    """根因假设。证据引用不在本实体上（R2），通过 HypothesisEvidence 关联表表达。"""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    id: str = Field(default_factory=lambda: _new_id("h"))
    investigation_run_id: str
    candidate_service: str
    candidate_component: str | None = None
    fault_type: str
    failure_mechanism: str
    predicted_observations: list[str]
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    rank: int | None = None


class RCAReport(BaseModel):
    """结构化诊断报告。诊断状态三选一：SUPPORTED / REFUTED / INCONCLUSIVE。"""

    model_config = ConfigDict(validate_assignment=True)

    investigation_run_id: str
    diagnosis_status: Literal["SUPPORTED", "REFUTED", "INCONCLUSIVE"]
    summary: str
    hypotheses: list[RootCauseHypothesis] = Field(default_factory=list)
    hypothesis_evidence: list[HypothesisEvidence] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    affected_services: list[str] = Field(
        default_factory=list,
        description="影响范围：因根因服务故障而受波及的上游服务（来自拓扑 blast radius）",
    )
    propagation_chain: list[dict] = Field(
        default_factory=list,
        description=(
            "传播路径：故障沿拓扑传导的节点序列（dict 形式，见 rca/chain.py 的 PropagationNode）。"
            "与 hypotheses 是两个概念：链上受影响节点不需要有独立故障机制，但必须有证据支撑。"
        ),
    )
    missing_data: list[str] = Field(default_factory=list)
    # 「查过了，确实没有」的查询。
    #
    # 为什么必须和 missing_data 分开：
    #   - missing_data = **没法查**（数据源挂了 / 查询失败）→ 禁止 REFUTED
    #   - no_data      = **查了，没有**（查询成功，结果为空）→ 阴性观测，
    #     可以支持 REFUTED（"如果真是池耗尽，日志里该有超时，但查了没有"）
    #
    # 把两者混为一谈就是把"没看见"当成"看见了" —— aurora-lab 记录的
    # "absence of evidence into fabricated findings" 正是这个 bug 的下游。
    no_data: list[str] = Field(default_factory=list)
    abstained: bool = False
    # 根因**开始**的时间。来自 rank1 候选支持证据的最早 observed_at，
    # 没有支持证据时为 None（**不拿 fault_start 充数**）。
    #
    # 为什么必须区分：fault_start 是告警**声称**的时间，observed_at 是
    # 我们实际**观测到**的时间。报告只能陈述观测到的事实。
    # OpenRCA 的答案是三元素（时间±60s / 组件 / 原因），缺少时间这一维
    # 会让我们的答案粒度低一档，也无从度量时间准确度。
    root_cause_time: datetime | None = None
    # 产出本报告的调查策略 digest（见 `evaluation/investigation_profile.py`）。
    #
    # 为什么必须记录：两次调查同一事故结论不同时，要能回答
    # "是策略变了还是数据变了"。没有这个字段就只能靠猜。
    profile_digest: str | None = None
    # 影子判定：某些闸门被要求"只观察不生效"时，把它们本来会下的结论记在这里。
    #
    # 为什么必须进报告而不是只打日志：这是**唯一**能回答"这个弃权闸门拦得对不对"
    # 的证据。只写日志的话，问题一旦发生就已经过去了，无从回看。
    # 每条形如 {"gate", "would_be", "reason", "reason_text", "detail"}。
    shadow_decisions: list[dict] = Field(default_factory=list)
    # 被丢掉的模型提案（带原因码）。
    #
    # 静默丢弃会让"模型提了但没用上"和"模型什么也没提"在事后完全无法区分 ——
    # 而这两件事的修法相反：前者要放宽校验，后者要改进提示词或输入。
    # 每条形如 {"candidate_service", "fault_type", "reason", "reason_text"}。
    rejected_proposals: list[dict] = Field(default_factory=list)
    # 决策策略的内容指纹（见 `traceops/runtime/strategy.py`）。
    #
    # 与 `profile_digest` 的分工：`profile_digest` 是**评测剖面**的身份
    # （消融实验比较用，由 `evaluation/investigation_profile.py` 产出）；
    # 这里是**线上实际生效的策略**的身份，包含阈值、排序权重、闸门开关与
    # 提示词版本。两次报告只有 `strategy_digest` 相等才可比 —— 否则
    # "结论变了"可能只是"规则变了"。
    strategy_digest: str | None = None
    # 本次运行的实际输入（预算、采集深度、拓扑指纹、提示词版本等）。
    #
    # 与 `strategy_digest` 分开的理由：预算从 50 改成 60 不该让两次结论
    # 变得"不可比"，而采集深度变了则必须知道 —— 两者语义不同，
    # 曾经被塞进同一批版本字符串里，于是既看不出一致也看不出差异。
    run_snapshot: dict = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=_utcnow)

    def all_evidence_ids(self) -> set[str]:
        """报告引用的全部证据 ID（证据实体 ∪ 假设-证据关联中出现的 ID）。"""
        ids = {ev.id for ev in self.evidence}
        ids |= {he.evidence_id for he in self.hypothesis_evidence}
        return ids
