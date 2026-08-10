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


class TestR007ComponentSpecificity:
    """R-007：给出了具体组件的候选优先。

    没有这条规则时，同分候选按 hypothesis_id（即 UUID）排序 = 随机。
    结果是"定位对了服务、但故障机制选错"，FT@1 会系统性下滑
    （F-001 实测从 1.00 掉到 0.60）。
    """

    def test_specific_component_wins_tie(self):
        hyps = [make_hypothesis("h-vague"), make_hypothesis("h-specific")]
        signals = [
            sig("h-vague"),
            sig("h-specific", has_specific_component=True),
        ]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].hypothesis_id == "h-specific"
        assert "R-007" in ranked[0].applied_rules

    def test_vague_does_not_get_r007(self):
        hyps = [make_hypothesis("h-vague")]
        ranked = rank_hypotheses(hyps, [sig("h-vague", has_specific_component=False)])
        assert "R-007" not in ranked[0].applied_rules

    def test_r007_does_not_override_stronger_signals(self):
        """具体性只是小加分（+1），不能压过配置变更（+3）。"""
        hyps = [make_hypothesis("h-vague-with-config"), make_hypothesis("h-specific-no-config")]
        signals = [
            sig("h-vague-with-config", config_change_precedes_fault=True, has_specific_component=False),
            sig("h-specific-no-config", has_specific_component=True),
        ]
        ranked = rank_hypotheses(hyps, signals)
        assert ranked[0].hypothesis_id == "h-vague-with-config"

    def test_specificity_ordering_is_deterministic(self):
        """同一输入两次排序必须一致（UUID 排序会违反这一点）。"""
        hyps = [make_hypothesis("h-a"), make_hypothesis("h-b"), make_hypothesis("h-c")]
        signals = [
            sig("h-a", has_specific_component=True),
            sig("h-b"),
            sig("h-c", has_specific_component=True),
        ]
        first = rank_hypotheses(hyps, signals)
        second = rank_hypotheses(list(reversed(hyps)), signals)
        assert [r.hypothesis_id for r in first] == [r.hypothesis_id for r in second]


class TestDeterministicTieBreak:
    """同分排序必须完全确定 —— 不能靠 UUID。

    这是可复现性的底线：同一场景两次运行若给出不同根因，
    既无法调试，也会让评估指标随测试执行顺序漂移（实际踩过：
    FT@1 在混合跑测试时从 1.00 掉到 0.60）。
    """

    def test_tie_breaks_by_service_then_component_then_fault_type(self):
        # 三个假设完全同分，只在服务名/组件/机制上不同
        hyps = [
            RootCauseHypothesis(
                investigation_run_id="run-001", candidate_service="svc-b",
                fault_type="t", failure_mechanism="m", predicted_observations=[]),
            RootCauseHypothesis(
                investigation_run_id="run-001", candidate_service="svc-a",
                candidate_component="comp", fault_type="t", failure_mechanism="m",
                predicted_observations=[]),
            RootCauseHypothesis(
                investigation_run_id="run-001", candidate_service="svc-a",
                fault_type="u", failure_mechanism="m", predicted_observations=[]),
        ]
        signals = [
            sig(hyps[0].id, supporting_evidence_count=1),
            sig(hyps[1].id, supporting_evidence_count=1),
            sig(hyps[2].id, supporting_evidence_count=1),
        ]
        ranked = rank_hypotheses(hyps, signals)
        # svc-a 排 svc-b 前；同服务内有组件的排前面
        assert ranked[0].hypothesis_id == hyps[1].id
        assert ranked[1].hypothesis_id == hyps[2].id
        assert ranked[2].hypothesis_id == hyps[0].id

    def test_same_input_same_order_regardless_of_list_order(self):
        hyps = [
            RootCauseHypothesis(investigation_run_id="r", candidate_service=f"svc-{i}",
                                fault_type="t", failure_mechanism="m",
                                predicted_observations=[])
            for i in range(5)
        ]
        signals = [sig(h.id, supporting_evidence_count=1) for h in hyps]
        first = [r.hypothesis_id for r in rank_hypotheses(hyps, signals)]
        second = [r.hypothesis_id for r in rank_hypotheses(list(reversed(hyps)), list(reversed(signals)))]
        assert first == second
