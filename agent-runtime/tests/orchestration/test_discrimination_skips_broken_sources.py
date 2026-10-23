"""判别回路不该在已知坏掉的数据源上再花一轮。

回路存在的理由是"这两个候选分不开"，而不是"再试一次"。当 prometheus 上一趟
已经失败时，聚焦错误率根本问不到 —— 再问一遍只会多花预算、并在报告里
多两条 missing_data 噪音，让"分不开"和"问不到"混成一个结论。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import HypothesisStatus, RootCauseHypothesis
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest,
    RunContext,
    _discrimination_decision,
)
from traceops.rca.ranking import RankedCandidate
from traceops.runtime.budget import BudgetManager

TS = datetime(2026, 3, 12, 14, 32, tzinfo=UTC)


def _request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id="run-1", incident_id="inc-1", environment="test",
        service_hint="checkout-service", fault_start=TS,
        window_start=TS - timedelta(minutes=10), window_end=TS + timedelta(minutes=5),
    )


def _hypothesis(hid: str, service: str, fault_type: str) -> RootCauseHypothesis:
    return RootCauseHypothesis(investigation_run_id="run-1", candidate_service=service,
                               fault_type=fault_type, failure_mechanism="m",
                               predicted_observations=[])


def _tied() -> tuple[list[RootCauseHypothesis], list[RankedCandidate]]:
    """两个服务、同分、同为 SUPPORTED —— 这才是"分不开"。"""
    hypotheses = [
        _hypothesis("h1", "payment-service", "latency_degradation"),
        _hypothesis("h2", "inventory-service", "latency_degradation"),
    ]
    # 必须用真实 id 关联：`RootCauseHypothesis` 自己生成 id，
    # 手写 "h1"/"h2" 会让 `by_id` 查不到，于是"并列"永远为空。
    ranked = [
        RankedCandidate(hypothesis_id=h.id, score=3, rank=index + 1,
                        status=HypothesisStatus.SUPPORTED, applied_rules=[])
        for index, h in enumerate(hypotheses)
    ]
    return hypotheses, ranked


class _Adapters:
    """只声明能力，不真取数：判别决策只关心"这个源还能不能问"。"""

    capabilities = frozenset({"metric_series", "error_rate", "logs", "traces"})


def _ctx() -> RunContext:
    return RunContext(store=EvidenceStore(), adapters=_Adapters(),
                      budget=BudgetManager(max_tool_calls=50))


def _state(failed: list[str]) -> dict:
    hypotheses, ranked = _tied()
    return {"request": _request(), "round": 0, "discriminated": [],
            "ranked": ranked, "hypotheses": hypotheses, "evidence": [],
            "failed_sources": failed}


class TestBrokenSourcesShortCircuitTheLoop:
    def test_a_failed_prometheus_means_no_focused_round(self):
        state = _state(["prometheus"])
        can, plan = _discrimination_decision(state, _ctx(), ranked=state["ranked"])

        assert can is False
        assert plan == []

    def test_an_unrelated_broken_source_does_not_block_the_round(self):
        state = _state(["loki"])
        can, plan = _discrimination_decision(state, _ctx(), ranked=state["ranked"])

        assert can is True
        assert all(item["source_id"] != "loki" for item in plan)
        assert any(item["source_id"] == "prometheus" for item in plan)

    def test_the_loop_still_respects_the_round_cap(self):
        state = _state([])
        state["round"] = 2
        assert _discrimination_decision(state, _ctx(), ranked=state["ranked"]) == (False, [])

    def test_a_separated_pair_is_not_discriminated(self):
        hypotheses, ranked = _tied()
        ranked[1] = RankedCandidate(hypothesis_id=hypotheses[1].id, score=2, rank=2,
                                    status=HypothesisStatus.SUPPORTED, applied_rules=[])
        state = {"request": _request(), "round": 0, "discriminated": [], "ranked": ranked,
                 "hypotheses": hypotheses, "evidence": [], "failed_sources": []}
        assert _discrimination_decision(state, _ctx(), ranked=ranked) == (False, [])
