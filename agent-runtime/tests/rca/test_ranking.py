"""Cycle 6 RED: 规则排序 R-001~R-006 (REVISIONS-1.1 R3)。

R3 决议：加权评分公式正式移除，P0 用确定性规则表做候选排序与假设验证。
规则命中 ≠ 确定根因——规则只负责排序和状态分类。

    规则   判断条件                   结果
    R-001  候选服务没有异常观测         降低优先级
    R-002  候选组件在故障前出现异常     提高优先级
    R-003  Trace 显示该组件调用异常     提高优先级
    R-004  存在故障前关联配置变更       提高优先级
    R-005  关键预测观测与假设冲突       降低优先级或反驳
    R-006  没有足够证据                标记为不确定
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import (
    Evidence,
    EvidenceType,
    HypothesisStatus,
    RelationType,
    RootCauseHypothesis,
)
from traceops.rca.ranking import CandidateSignals, RankedCandidate, rank_hypotheses

TS = datetime(2026, 10, 30, 2, 0, 0, tzinfo=UTC)


def make_hypothesis(hid: str, service: str = "redis") -> RootCauseHypothesis:
    return RootCauseHypothesis(
        id=hid, investigation_run_id="inv-001",
        candidate_service=service, candidate_component="connection-pool",
        fault_type="pool_exhaustion", failure_mechanism="saturation",
        predicted_observations=["pool_pending_increased"],
    )


def sig(hid: str, **overrides) -> CandidateSignals:
    base = dict(
        hypothesis_id=hid,
        has_anomaly_observation=True,
        anomaly_precedes_fault=False,
        trace_anomaly=False,
        config_change_precedes_fault=False,
        contradicting_evidence_count=0,
        supporting_evidence_count=1,
    )
    base.update(overrides)
    return CandidateSignals(**base)


class TestRules:
    def test_r001_no_anomaly_demoted(self):
        hyps = [make_hypothesis("h-anomalous"), make_hypothesis("h-clean")]
        signals = [sig("h-anomalous"), sig("h-clean", has_anomaly_observation=False, supporting_evidence_count=0)]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].hypothesis_id == "h-anomalous"
        assert "R-001" in ranked[1].applied_rules
        assert ranked[1].status == HypothesisStatus.INCONCLUSIVE  # R-006 also fires (no evidence of any kind)

    def test_r002_preceding_anomaly_raises(self):
        hyps = [make_hypothesis("h-a"), make_hypothesis("h-b")]
        signals = [sig("h-a"), sig("h-b", anomaly_precedes_fault=True)]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].hypothesis_id == "h-b"
        assert "R-002" in ranked[0].applied_rules

    def test_r003_trace_anomaly_raises(self):
        hyps = [make_hypothesis("h-a"), make_hypothesis("h-b")]
        signals = [sig("h-a"), sig("h-b", trace_anomaly=True)]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].hypothesis_id == "h-b"
        assert "R-003" in ranked[0].applied_rules

    def test_r004_config_change_is_strongest_signal(self):
        hyps = [make_hypothesis("h-trace"), make_hypothesis("h-config")]
        signals = [
            sig("h-trace", trace_anomaly=True),
            sig("h-config", config_change_precedes_fault=True),
        ]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].hypothesis_id == "h-config"

    def test_r005_contradiction_refutes(self):
        hyps = [make_hypothesis("h-redis")]
        signals = [sig("h-redis", contradicting_evidence_count=3, supporting_evidence_count=1)]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].status == HypothesisStatus.REFUTED
        assert "R-005" in ranked[0].applied_rules

    def test_r005_mild_contradiction_demotes_without_refuting(self):
        hyps = [make_hypothesis("h-a"), make_hypothesis("h-b")]
        signals = [
            sig("h-a", supporting_evidence_count=2),
            sig("h-b", supporting_evidence_count=2, contradicting_evidence_count=1),
        ]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].hypothesis_id == "h-a"
        assert ranked[1].status != HypothesisStatus.REFUTED

    def test_r006_no_evidence_inconclusive(self):
        hyps = [make_hypothesis("h-empty")]
        signals = [sig("h-empty", supporting_evidence_count=0, has_anomaly_observation=False)]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].status == HypothesisStatus.INCONCLUSIVE
        assert "R-006" in ranked[0].applied_rules

    def test_supported_requires_supporting_evidence(self):
        hyps = [make_hypothesis("h-a"), make_hypothesis("h-b")]
        signals = [
            sig("h-a", supporting_evidence_count=2, trace_anomaly=True),
            sig("h-b", supporting_evidence_count=0),
        ]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].status == HypothesisStatus.SUPPORTED
        assert ranked[1].status == HypothesisStatus.INCONCLUSIVE


class TestRankingShape:
    def test_ranks_are_sequential(self):
        hyps = [make_hypothesis(f"h-{i}") for i in range(4)]
        signals = [sig(f"h-{i}") for i in range(4)]
        ranked = rank_hypotheses(hyps, signals)
        assert [r.rank for r in ranked] == [1, 2, 3, 4]

    def test_deterministic_tiebreak_by_hypothesis_id(self):
        hyps = [make_hypothesis("h-z"), make_hypothesis("h-a")]
        signals = [sig("h-z"), sig("h-a")]
        r1 = rank_hypotheses(hyps, signals)
        r2 = rank_hypotheses(list(reversed(hyps)), list(reversed(signals)))
        assert [r.hypothesis_id for r in r1] == [r.hypothesis_id for r in r2]

    def test_unknown_hypothesis_signals_rejected(self):
        hyps = [make_hypothesis("h-a")]
        with pytest.raises(ValueError, match="signal"):
            rank_hypotheses(hyps, [sig("h-missing")])
