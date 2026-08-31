"""机会校正指标：证明 AC@5=1.00 在候选池很小时是伪指标。

实测背景：F-002/F-003/F-004 各只生成 1 个候选服务，因此 rank<=3、rank<=5
恒真。本测试用**真实候选池大小**验证这一点，并要求指标显式暴露它。
"""
from __future__ import annotations

import pytest

from evaluation.chance import chance_at, compute_chance_metrics, lift_at
from evaluation.metrics import ScenarioOutcome


def _outcome(pool: int, ac1=True, ac3=True, ac5=True, fault=True, sid="S"):
    return ScenarioOutcome(
        scenario_id=sid, service_ac_at_1=ac1, service_ac_at_3=ac3,
        service_ac_at_5=ac5, fault_type_at_1=True, component_at_1=True,
        diagnosis_correct=True, abstained=False, evidence_recall=1.0,
        citation_precision=1.0, cited_evidence=2, fault_injected=fault,
        chain_declared=False, chain_order_correct=None,
        affected_services_correct=None, tool_calls=3, tokens_used=0,
        candidate_pool_size=pool,
    )


class TestChanceBaseline:
    def test_single_candidate_pool_makes_chance_perfect(self):
        """池子只有 1 个候选 → 随机必中，Chance=1.0，Top-k 无区分力。"""
        assert chance_at(1, 1) == 1.0
        assert chance_at(1, 5) == 1.0

    def test_larger_pool_lowers_chance(self):
        assert chance_at(10, 1) == pytest.approx(0.1)
        assert chance_at(10, 5) == pytest.approx(0.5)

    def test_zero_pool_has_no_baseline(self):
        assert chance_at(0, 5) == 0.0


class TestLiftExposesPseudoMetric:
    def test_lift_is_none_when_chance_is_perfect(self):
        """候选池 1 个：AC=1.00 但 Lift 无意义 —— 必须返回 None 而不是 1.0。

        返回 1.0 会让人误读成"恰好等于随机水平"，实际是"没有可比基线"。
        """
        assert lift_at(1.0, 1.0) is None

    def test_lift_computed_against_real_baseline(self):
        assert lift_at(0.8, 0.1) == pytest.approx(8.0)


class TestAggregatedChanceMetrics:
    def test_pool_size_comes_from_real_outcomes(self):
        metrics = compute_chance_metrics([_outcome(1), _outcome(1), _outcome(1)])
        assert metrics.candidate_pool_size == 1
        # 池子只有 1 → AC@3/@5 的 Lift 必须是 None（伪指标）
        assert metrics.lift_at_3 is None
        assert metrics.lift_at_5 is None

    def test_healthy_control_excluded_from_denominator(self):
        metrics = compute_chance_metrics([_outcome(1, fault=False), _outcome(10)])
        assert metrics.candidate_pool_size == 10

    def test_empty_input_is_safe(self):
        metrics = compute_chance_metrics([])
        assert metrics.candidate_pool_size == 0
        assert metrics.lift_at_1 is None
