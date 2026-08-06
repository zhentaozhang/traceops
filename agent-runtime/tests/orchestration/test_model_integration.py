"""模型接入编排图的边界测试。

关键边界：
- model=None（默认）→ 行为与 P0 完全一致，零 token 消耗
- 模型失败/预算耗尽 → 只记 missing_data，调查继续
- 模型提案进入同一套规则排序，不自带排名
- 模型不能提出超出白名单的服务
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import EvidenceType
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetManager
from traceops.runtime.model import (
    DeterministicModel,
    ModelHypothesis,
    ScriptedModel,
)

FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 30, 10, 6, 0, tzinfo=UTC)


class OkAdapters:
    def query_service_error_rate(self, *, service, start, end):
        ts = int(FAULT_START.timestamp())
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": service}, "values": [[ts - 60, "0.01"], [ts, "0.4"]]}]}}

    def query_service_logs(self, *, service, start, end, filter=None):
        ns = int(FAULT_START.timestamp() * 1e9)
        return {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": service, "level": "error"},
             "values": [[str(ns), "ERROR redis timeout - pool exhausted"]]}]}}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        return {"traces_considered": 1, "batches": [{
            "resource": {"service.name": service},
            "spans": [{"traceID": "a" * 32, "spanID": "1" * 16, "operationName": "redis.command",
                       "startTime": int(FAULT_START.timestamp() * 1e6), "duration": 2_000_000}]}]}

    def query_metric_series(self, *, service, metric, start, end):
        raise RuntimeError("unused")


def request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id="run-model-001", incident_id="inc-model-001",
        environment="demo", service_hint="order-service",
        fault_start=FAULT_START, window_start=WINDOW_START, window_end=WINDOW_END,
    )


def context(model=None, budget=None) -> RunContext:
    return RunContext(store=EvidenceStore(), adapters=OkAdapters(),
                      budget=budget or BudgetManager(max_tool_calls=10), model=model)


class TestNoModelIsUnchanged:
    def test_default_context_uses_no_model(self):
        ctx = context()
        result = run_investigation(request(), ctx)
        assert ctx.budget.tokens_used == 0, "P0 must not consume model tokens"
        assert result.report is not None

    def test_deterministic_model_consumes_nothing(self):
        ctx = context(model=DeterministicModel())
        run_investigation(request(), ctx)
        assert ctx.budget.tokens_used == 0


class TestModelProposalsJoinRanking:
    def test_model_hypothesis_appears_in_report(self):
        """模型的真实价值：在证据已知的服务上提出**不同故障机制**。
        （服务归属由证据决定，模型不该凭空引入没有证据的服务——那个用例见下面的白名单测试）"""
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="order-service", fault_type="thread_pool_exhaustion",
                            failure_mechanism="sync calls blocking the request thread")])
        ctx = context(model=model)

        result = run_investigation(request(), ctx)

        assert result.report is not None
        types = {h.fault_type for h in result.report.hypotheses}
        assert "thread_pool_exhaustion" in types

    def test_model_hypothesis_has_no_rank_of_its_own(self):
        """排名由规则层给出，模型不能自带排名 —— 检查转换后的领域对象。"""
        from traceops.runtime.model import hypothesis_to_domain
        hypothesis = hypothesis_to_domain(
            ModelHypothesis(candidate_service="mysql", fault_type="t", failure_mechanism="m"),
            investigation_run_id="run-001")
        assert hypothesis.rank is None

    def test_model_tokens_are_recorded(self):
        ctx = context(model=ScriptedModel(tokens=(400, 150))
                      )
        run_investigation(request(), ctx)
        assert ctx.budget.tokens_used == 550

    def test_duplicate_proposal_does_not_create_parallel_candidate(self):
        # 规则检测器已会提出 redis/pool_exhaustion/redis-connection-pool
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="redis", fault_type="pool_exhaustion",
                            failure_mechanism="dup", candidate_component="redis-connection-pool")])
        ctx = context(model=model)

        result = run_investigation(request(), ctx)

        keys = [(h.candidate_service, h.fault_type, h.candidate_component)
                for h in result.report.hypotheses]
        assert len(keys) == len(set(keys)), "duplicates must be collapsed"


class TestModelFailuresDoNotStopInvestigation:
    def test_model_unavailable_is_recorded_but_investigation_continues(self):
        ctx = context(model=ScriptedModel(fail=True))

        result = run_investigation(request(), ctx)

        assert result.status in ("SUPPORTED", "INCONCLUSIVE")
        assert result.report is not None
        assert any("model unavailable" in note for note in result.report.missing_data)

    def test_model_budget_exhausted_is_recorded_not_fatal(self):
        budget = BudgetManager(max_tool_calls=10, max_tokens=500)
        ctx = context(model=ScriptedModel(tokens=(400, 200)), budget=budget)

        result = run_investigation(request(), ctx)

        assert result.report is not None, "model budget exhaustion must not kill the run"
        assert not any("budget exhausted" in e for e in result.errors)

    def test_model_cannot_propose_service_outside_allowlist(self):
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="payment-service", fault_type="timeout",
                            failure_mechanism="fabricated")])
        ctx = context(model=model)

        result = run_investigation(request(), ctx)

        services = {h.candidate_service for h in result.report.hypotheses}
        assert "payment-service" not in services


class TestEvidenceSummaryIsMinimal:
    def test_model_receives_counts_not_raw_payload(self):
        model = ScriptedModel()
        ctx = context(model=model)
        run_investigation(request(), ctx)

        assert model.calls, "model should have been called"
        summary = model.calls[0].evidence_summary
        assert isinstance(summary, dict)
        # 只应有类型键与 count/services，不含原始日志文本
        joined = str(summary)
        assert "pool exhausted" not in joined
        assert "ERROR" not in joined
        for entry in summary.values():
            assert set(entry) <= {"count", "services"}
