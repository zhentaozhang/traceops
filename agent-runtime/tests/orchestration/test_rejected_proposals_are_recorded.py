"""被丢掉的模型提案必须留痕。

## 原来的行为

`ModelGateway._validate` 里三条 `continue`：缺 `candidate_service`、缺
`fault_type`、服务不在白名单。三条都**静默**丢弃，`graph._hypothesize_node`
里"与规则假设重复"那条 `continue` 也是静默的。

后果是报告里只剩一个 `hypotheses` 列表。看到"模型只提了一条"时，无法判断是：

- 模型只提了一条（**提示词/输入的问题**，该改提示词或给更多证据）
- 模型提了五条、四条被白名单挡掉（**校验过严的问题**，该放宽 allowed_services）

这两件事的修法相反，而原实现把它们压成同一个现象。super-agent 的做法是给
每个被拒绝的动作一个原因码（20 个 `REASONS`）+ 一条审计记录，这里搬最小版本：
被丢弃的提案带原因码进 `RCAReport.rejected_proposals`。

## 不改变结论

`rejected_proposals` 只用于事后解释，不参与排序、不进 `diagnosis_status`，
和 `missing_data` 一个层级。
"""
from __future__ import annotations

from datetime import UTC, datetime

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetManager
from traceops.runtime.model import ModelHypothesis, ScriptedModel

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


def _request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id="run-rej-001", incident_id="inc-rej-001",
        environment="demo", service_hint="order-service",
        fault_start=FAULT_START, window_start=WINDOW_START, window_end=WINDOW_END,
    )


def _run(model):
    ctx = RunContext(store=EvidenceStore(), adapters=OkAdapters(),
                     budget=BudgetManager(max_tool_calls=10), model=model)
    return run_investigation(_request(), ctx)


class TestOutOfScopeProposalIsRecorded:
    def test_service_outside_allowlist_leaves_a_reason(self):
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="payment-service", fault_type="timeout",
                            failure_mechanism="fabricated")])

        result = _run(model)

        rejected = result.report.rejected_proposals
        assert len(rejected) == 1, f"模型提案被丢弃却没有记录：{rejected}"
        assert rejected[0]["candidate_service"] == "payment-service"
        assert rejected[0]["reason"] == "service_out_of_scope"
        assert rejected[0]["reason_text"], "原因码必须有人类可读的解释"

    def test_a_rejection_does_not_change_the_diagnosis(self):
        """留痕只用于解释，不能影响结论。"""
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="payment-service", fault_type="timeout",
                            failure_mechanism="fabricated")])

        result = _run(model)

        services = {h.candidate_service for h in result.report.hypotheses}
        assert "payment-service" not in services


class TestIncompleteProposalIsRecorded:
    def test_missing_candidate_service_says_so(self):
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="", fault_type="timeout",
                            failure_mechanism="half-written")])

        result = _run(model)

        reasons = {r["reason"] for r in result.report.rejected_proposals}
        assert "missing_candidate_service" in reasons

    def test_missing_fault_type_says_so(self):
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="order-service", fault_type="",
                            failure_mechanism="half-written")])

        result = _run(model)

        reasons = {r["reason"] for r in result.report.rejected_proposals}
        assert "missing_fault_type" in reasons


class TestDuplicateOfRuleHypothesisIsRecorded:
    def test_duplicate_still_appears_in_rejected_proposals(self):
        """规则检测器已会提出 order-service/availability_degradation。

        模型提出完全相同的三元组时必须留痕 —— 原先是静默 `continue`，
        于是"模型提了一条被去重"和"模型什么也没提"在报告里长得一模一样。
        """
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="order-service",
                            fault_type="availability_degradation",
                            failure_mechanism="dup")])

        result = _run(model)

        reasons = [r["reason"] for r in result.report.rejected_proposals]
        assert "duplicate_of_rule_hypothesis" in reasons
        # 去重本身不受影响：仍然只保留一份
        keys = [(h.candidate_service, h.fault_type, h.candidate_component)
                for h in result.report.hypotheses]
        assert len(keys) == len(set(keys))


class TestEmptyRejectionListMeansNothingWasDropped:
    def test_accepted_proposal_produces_no_rejection(self):
        model = ScriptedModel(hypotheses=[
            ModelHypothesis(candidate_service="order-service",
                            fault_type="thread_pool_exhaustion",
                            failure_mechanism="sync calls blocking the request thread")])

        result = _run(model)

        fault_types = {h.fault_type for h in result.report.hypotheses}
        assert "thread_pool_exhaustion" in fault_types
        assert result.report.rejected_proposals == [], (
            "提案被接受就不该出现在拒绝记录里，否则两条清单会自相矛盾")

    def test_no_model_produces_no_rejection(self):
        result = _run(None)
        assert result.report.rejected_proposals == []
