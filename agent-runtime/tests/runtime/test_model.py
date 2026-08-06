"""模型接口测试：预算、白名单、降级（蓝图 §8.1 / R6）。"""
from __future__ import annotations

import pytest

from traceops.runtime.budget import BudgetExhausted, BudgetManager
from traceops.runtime.model import (
    DeterministicModel,
    ModelGateway,
    ModelHypothesis,
    ModelRequest,
    ModelUnavailable,
    ScriptedModel,
    hypothesis_to_domain,
)

REQUEST = ModelRequest(
    incident_id="inc-001",
    service_hint="order-service",
    evidence_summary={"metrics": 1, "logs": 1, "traces": 1},
    allowed_services=["order-service", "redis", "mysql"],
)


class TestDeterministicDefault:
    def test_default_model_proposes_nothing(self):
        response = DeterministicModel().propose(REQUEST)
        assert response.hypotheses == []
        assert response.total_tokens == 0

    def test_no_confident_field_on_model_hypothesis(self):
        """蓝图明确禁止第一版用 LLM 自评 confidence —— 数据结构层面就不给。"""
        fields = set(ModelHypothesis.__dataclass_fields__)
        assert "confidence" not in fields
        assert "score" not in fields


class TestBudgetProtection:
    def test_call_records_actual_tokens(self):
        budget = BudgetManager(max_tokens=100_000)
        gateway = ModelGateway(ScriptedModel(tokens=(500, 200)), budget)

        gateway.propose(REQUEST)

        assert budget.tokens_used == 700
        assert budget.tokens_remaining == 99_300

    def test_budget_exhaustion_prevents_the_call_entirely(self):
        budget = BudgetManager(max_tokens=1000)
        model = ScriptedModel(tokens=(500, 200))
        gateway = ModelGateway(model, budget, estimate_tokens=2000)

        with pytest.raises(BudgetExhausted):
            gateway.propose(REQUEST)

        assert model.calls == [], "model must not be invoked when budget is insufficient"

    def test_failed_call_releases_reservation(self):
        budget = BudgetManager(max_tokens=100_000)
        gateway = ModelGateway(ScriptedModel(fail=True), budget, estimate_tokens=5000)

        with pytest.raises(ModelUnavailable):
            gateway.propose(REQUEST)

        # 调用失败不应消耗预算，否则重试会把预算烧光
        assert budget.tokens_used == 0
        assert budget.tokens_remaining == 100_000

    def test_failed_call_does_not_block_later_calls(self):
        budget = BudgetManager(max_tokens=1000)
        failing = ScriptedModel(fail=True)
        gateway = ModelGateway(failing, budget, estimate_tokens=800)
        with pytest.raises(ModelUnavailable):
            gateway.propose(REQUEST)

        # 预留已释放，后续调用仍可进行
        gateway2 = ModelGateway(ScriptedModel(tokens=(100, 50)), budget, estimate_tokens=800)
        gateway2.propose(REQUEST)
        assert budget.tokens_used == 150


class TestOutputValidation:
    def test_hypothesis_outside_allowlist_is_dropped(self):
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="redis", fault_type="pool_exhaustion", failure_mechanism="x"),
            ModelHypothesis(candidate_service="payment-service", fault_type="timeout", failure_mechanism="y"),
        ])
        gateway = ModelGateway(model, BudgetManager())

        response = gateway.propose(REQUEST)

        assert [h.candidate_service for h in response.hypotheses] == ["redis"]

    def test_empty_allowlist_accepts_all(self):
        request = ModelRequest(incident_id="inc-001", service_hint="x",
                               evidence_summary={}, allowed_services=[])
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="anything", fault_type="t", failure_mechanism="m")])
        gateway = ModelGateway(model, BudgetManager())

        response = gateway.propose(request)

        assert len(response.hypotheses) == 1

    def test_max_hypotheses_is_enforced(self):
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="redis", fault_type=f"t{i}", failure_mechanism="m")
            for i in range(6)
        ])
        request = ModelRequest(incident_id="inc-001", service_hint="order-service",
                               evidence_summary={}, allowed_services=["redis"], max_hypotheses=2)
        gateway = ModelGateway(model, BudgetManager())

        response = gateway.propose(request)

        assert len(response.hypotheses) == 2

    def test_hypothesis_missing_required_fields_is_dropped(self):
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="", fault_type="t", failure_mechanism="m"),
            ModelHypothesis(candidate_service="redis", fault_type="", failure_mechanism="m"),
            ModelHypothesis(candidate_service="redis", fault_type="ok", failure_mechanism="m"),
        ])
        gateway = ModelGateway(model, BudgetManager())

        response = gateway.propose(REQUEST)

        assert len(response.hypotheses) == 1
        assert response.hypotheses[0].fault_type == "ok"


class TestDomainConversion:
    def test_model_hypothesis_starts_as_proposed(self):
        hypothesis = hypothesis_to_domain(
            ModelHypothesis(candidate_service="redis", fault_type="pool_exhaustion",
                            failure_mechanism="pool saturated", candidate_component="redis-connection-pool"),
            investigation_run_id="run-001",
        )
        assert hypothesis.status.value == "proposed"
        assert hypothesis.rank is None, "排序必须由规则层给出，模型不能自带排名"

    def test_conversion_carries_predicted_observations(self):
        hypothesis = hypothesis_to_domain(
            ModelHypothesis(candidate_service="redis", fault_type="t", failure_mechanism="m",
                            expected_observations=["pool_pending_increased"]),
            investigation_run_id="run-001",
        )
        assert hypothesis.predicted_observations == ["pool_pending_increased"]


class TestGatewayInfo:
    def test_model_info_exposes_budget_state(self):
        budget = BudgetManager(max_tokens=100_000)
        gateway = ModelGateway(ScriptedModel(tokens=(300, 100)), budget)
        gateway.propose(REQUEST)
        info = gateway.model_info()
        assert info["budget_used"] == 400
        assert info["name"] == "scripted"
