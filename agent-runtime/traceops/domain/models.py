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


class RelationType(str, Enum):
    """P0 只支持证据→假设关系。证据↔证据关系(PRECEDES/SAME_TRACE/...)延后 P1 (R2)。"""

    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"


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
    missing_data: list[str] = Field(default_factory=list)
    abstained: bool = False
    generated_at: datetime = Field(default_factory=_utcnow)

    def all_evidence_ids(self) -> set[str]:
        """报告引用的全部证据 ID（证据实体 ∪ 假设-证据关联中出现的 ID）。"""
        ids = {ev.id for ev in self.evidence}
        ids |= {he.evidence_id for he in self.hypothesis_evidence}
        return ids
