"""记忆能推翻并列，但永远不能单独断定根因。

## 两个方向都要测

**能调权**：两个候选分数完全相同时，唯一还有信息量的维度是"以前是谁"。
这正是 `_ambiguous_candidates` 会判成"并列"然后停下的那一步 ——
记忆加 1 分就能让它不再并列，不需要多跑一趟查询。

**不能定论**：记忆是**先验**，不是证据。一个没有任何观测的候选，
哪怕它以前出过一百次，也必须是 `INCONCLUSIVE`。
这条约束是结构性的（`_evaluate` 的状态分类要求
`supporting_evidence_count > 0` 才可能 SUPPORTED，而记忆不产生 link），
但结构性约束最怕被人"顺手优化掉"，所以这里也钉一次。

## 为什么被推翻的历史不该加分

如果"以前是它"总能加分，系统会锁死在同一个答案上：判错了 → 下次还判它 →
再判错。所以只有净值为正（命中多于推翻）才算"以前见过"。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import HypothesisStatus, RootCauseHypothesis
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.rca.ranking import CandidateSignals, _evaluate, derive_signals
from traceops.runtime.budget import BudgetManager
from traceops.runtime.incident_memory import IncidentMemory
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)
WINDOW_START = TS - timedelta(minutes=5)
WINDOW_END = TS


def _request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id="run-memory",
        incident_id="inc-memory",
        environment="demo",
        service_hint="checkout-service",
        fault_start=TS,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )


class _TiedErrorRate:
    """checkout 正常，payment 与 inventory 都异常 —— 两个候选同分。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def query_service_error_rate(self, service, start, end):
        self.calls.append(service)
        value = "0.42" if service in {"payment-service", "inventory-service"} else "0.001"
        return {"status": "success",
                "data": {"resultType": "matrix", "result": [
                    {"metric": {}, "values": [[(WINDOW_START + timedelta(seconds=30)).timestamp(),
                                               value]]}]}}

    def query_service_logs(self, service, start, end, filter=None):
        self.calls.append(service)
        return {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        self.calls.append(service)
        return {"traces_considered": 0, "batches": []}


def _context(adapters, memory: IncidentMemory | None) -> RunContext:
    return RunContext(
        store=EvidenceStore(),
        adapters=adapters,
        budget=BudgetManager(max_tool_calls=30),
        # 共享下游 → 相干 → 歧义闸门会触发（互不相干时闸门故意放行）
        topology=ServiceTopology.from_edges(
            [("checkout-service", "payment-service"),
             ("checkout-service", "inventory-service"),
             ("payment-service", "db"),
             ("inventory-service", "db")]),
        scope_depth=1,
        memory=memory,
    )


class TestMemoryBreaksTies:
    def test_without_memory_the_two_candidates_stay_ambiguous(self):
        result = run_investigation(_request(), _context(_TiedErrorRate(), None))

        assert result.report.diagnosis_status == "INCONCLUSIVE"
        assert result.report.priors == []

    def test_a_corroborated_prior_decides_the_tie(self):
        memory = IncidentMemory()
        memory.record(service="payment-service", fault_type="availability_degradation")

        result = run_investigation(_request(), _context(_TiedErrorRate(), memory))

        assert result.report.diagnosis_status == "SUPPORTED"
        assert "payment-service" in result.report.summary

    def test_the_prior_is_reported_so_reviewers_can_see_it(self):
        memory = IncidentMemory()
        memory.record(service="payment-service", fault_type="availability_degradation")

        result = run_investigation(_request(), _context(_TiedErrorRate(), memory))

        services = [entry["service"] for entry in result.report.priors]
        assert services == ["payment-service"]

    def test_a_refuted_prior_does_not_decide_the_tie(self):
        memory = IncidentMemory()
        memory.record(service="payment-service", fault_type="availability_degradation",
                      refuted=True)
        memory.record(service="payment-service", fault_type="availability_degradation",
                      refuted=True)

        result = run_investigation(_request(), _context(_TiedErrorRate(), memory))

        # 以前判错过它，这次不该因为"老熟人"再判一次
        assert result.report.diagnosis_status == "INCONCLUSIVE"
        assert result.report.priors == []

    def test_a_supported_outcome_is_recorded_for_the_next_time(self):
        memory = IncidentMemory()
        memory.record(service="payment-service", fault_type="availability_degradation")

        run_investigation(_request(), _context(_TiedErrorRate(), memory))

        prior = memory.recall(service="payment-service",
                              fault_type="availability_degradation")
        # 1 次历史 + 1 次本次命中
        assert prior is not None and prior.hits == 2


class TestMemoryCannotConcludeAlone:
    def test_a_prior_alone_never_makes_a_candidate_supported(self):
        signals = CandidateSignals(hypothesis_id="h-1", previously_seen=True)

        score, status, rules = _evaluate(signals)

        assert status is HypothesisStatus.INCONCLUSIVE
        assert "R-010" in rules and "R-006" in rules
        # 降权与升权正好抵消：没有观测的候选最终是 0 分，不是正分
        assert score == 0

    def test_a_prior_alone_adds_no_supporting_evidence(self):
        hypothesis = RootCauseHypothesis(
            investigation_run_id="run-1",
            candidate_service="payment-service",
            fault_type="availability_degradation",
            failure_mechanism="connection pool exhausted",
            predicted_observations=[],
        )

        signals = derive_signals(
            hypothesis, [], EvidenceStore(), fault_start=TS, previously_seen=True)

        assert signals.previously_seen is True
        assert signals.supporting_evidence_count == 0
        assert signals.has_anomaly_observation is False
