"""EvidenceRelation 测试（REVISIONS-1.1 R2 的 P1 扩展）。

## R2 为什么把它们分开

P0 只需要「证据 → 假设」（HypothesisEvidence），因为诊断只需要知道
"这条证据支持哪个候选"。

但**影响分析与因果重建**需要另一类关系：「证据 ↔ 证据」——
A 是否早于 B、A 与 B 是否同一条链路、A 是否影响了 B 的服务。

把两类关系塞进一张表会出两类错误：
1. 语义错：`SUPPORTS` 的对象是假设，不是另一条证据
2. 查询错：想找"支持 H-001 的证据"时把证据间关系也捞出来

所以 P1 用独立的 `EvidenceRelation`，关系类型也完全不同。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from traceops.domain.models import (
    CreatedBy,
    Evidence,
    EvidenceRelation,
    EvidenceRelationType,
    EvidenceType,
)


class TestRelationTypes:
    def test_p1_types_declared(self):
        """R2 明确列出的四种证据间关系。"""
        assert EvidenceRelationType.PRECEDES.value == "PRECEDES"
        assert EvidenceRelationType.SAME_TRACE.value == "SAME_TRACE"
        assert EvidenceRelationType.CORRELATED_WITH.value == "CORRELATED_WITH"
        assert EvidenceRelationType.AFFECTS.value == "AFFECTS"

    def test_supports_is_not_an_evidence_relation(self):
        """SUPPORTS/CONTRADICTS 属于 HypothesisEvidence，不能混进这张表。"""
        assert "SUPPORTS" not in {r.value for r in EvidenceRelationType}
        assert "CONTRADICTS" not in {r.value for r in EvidenceRelationType}


class TestEvidenceRelation:
    def test_minimal_valid(self):
        relation = EvidenceRelation(
            source_evidence_id="e-1",
            target_evidence_id="e-2",
            relation_type=EvidenceRelationType.PRECEDES,
        )
        assert relation.relation_type is EvidenceRelationType.PRECEDES
        assert relation.created_by is CreatedBy.RULE  # 默认由规则产生

    def test_self_relation_rejected(self):
        """同一条证据不能和自己有关系 —— 那通常是构造 bug。"""
        with pytest.raises(ValidationError, match="itself"):
            EvidenceRelation(
                source_evidence_id="e-1", target_evidence_id="e-1",
                relation_type=EvidenceRelationType.PRECEDES,
            )

    def test_same_trace_requires_trace_id(self):
        """SAME_TRACE 关系没有 trace_id 就无从验证，必须拒绝。"""
        with pytest.raises(ValidationError, match="trace_id"):
            EvidenceRelation(
                source_evidence_id="e-1", target_evidence_id="e-2",
                relation_type=EvidenceRelationType.SAME_TRACE,
            )

    def test_precedes_carries_time_delta(self):
        relation = EvidenceRelation(
            source_evidence_id="e-1", target_evidence_id="e-2",
            relation_type=EvidenceRelationType.PRECEDES,
            time_delta_seconds=30.0,
        )
        assert relation.time_delta_seconds == 30.0

    def test_precedes_with_negative_delta_rejected(self):
        """PRECEDES 意味着 source 更早；负数自相矛盾。"""
        with pytest.raises(ValidationError, match="non-negative"):
            EvidenceRelation(
                source_evidence_id="e-1", target_evidence_id="e-2",
                relation_type=EvidenceRelationType.PRECEDES,
                time_delta_seconds=-5.0,
            )

    def test_correlation_strength_is_bounded(self):
        relation = EvidenceRelation(
            source_evidence_id="e-1", target_evidence_id="e-2",
            relation_type=EvidenceRelationType.CORRELATED_WITH,
            strength=0.8,
        )
        assert relation.strength == 0.8
        with pytest.raises(ValidationError):
            EvidenceRelation(
                source_evidence_id="e-1", target_evidence_id="e-2",
                relation_type=EvidenceRelationType.CORRELATED_WITH,
                strength=1.5,
            )

    def test_affects_targets_a_service_not_an_evidence(self):
        """AFFECTS 表达"这条证据影响了某个服务"，目标服务必填。"""
        relation = EvidenceRelation(
            source_evidence_id="e-1", target_evidence_id="e-2",
            relation_type=EvidenceRelationType.AFFECTS,
            affected_service="order-service",
        )
        assert relation.affected_service == "order-service"

    def test_affects_requires_affected_service(self):
        with pytest.raises(ValidationError, match="affected_service"):
            EvidenceRelation(
                source_evidence_id="e-1", target_evidence_id="e-2",
                relation_type=EvidenceRelationType.AFFECTS,
            )

    def test_serialisable(self):
        relation = EvidenceRelation(
            source_evidence_id="e-1", target_evidence_id="e-2",
            relation_type=EvidenceRelationType.PRECEDES,
            time_delta_seconds=30.0,
        )
        assert relation.model_dump(mode="json")["relation_type"] == "PRECEDES"
