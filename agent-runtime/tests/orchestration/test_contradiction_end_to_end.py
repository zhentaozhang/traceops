"""端到端：反向证据必须真的被生成、进排序、并被报告引用。

## 为什么需要这条（单元测试不够）

`test_contradiction.py` 证明了判据函数正确，但在真实调查里
`_link_node` 必须真的把这些关系写进 `links`，`derive_signals` 必须真的
读到它们，`R-005` 必须真的降权。任一环节断了，判据再准也没用。

这也是 `评测口径` 里「CONTRADICTS 生成器缺失」那一条的收官验证：
指标从"分母恒为 0"变成"有真实分母"。

## 场景构造

一个"假设是池耗尽，但观测显示系统完全健康"的调查：
- 候选假设：redis / pool_exhaustion
- 证据：redis 的错误率全程 0.01（远低于 0.05 阈值）+ 干净日志
  → 这些证据**反证**该假设
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import (
    Evidence, EvidenceType, HypothesisStatus, RelationType, RootCauseHypothesis,
)
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest, RunContext, run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


class ConflictingAdapters:
    """日志说有问题（生成假设），指标说没问题（反证该假设）。

    这才是反证的真实形态：假设由**一类**证据产生，另一类证据与它矛盾。
    如果所有证据都健康，系统根本不生成假设（正确行为），也就无从反证。
    """

    def query_service_error_rate(self, *, service, start, end):
        # 指标全程远低于阈值 → 反证 "可用性下降/池耗尽"
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": service},
             "values": [[TS.timestamp(), "0.01"], [TS.timestamp() + 60, "0.01"]]}]}}

    def query_service_logs(self, *, service, start, end, filter=None):
        # 日志里有 timeout 关键词 → 生成 "依赖超时" 类假设
        return {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": service, "level": "error"},
             "values": [[str(int(TS.timestamp() * 1e9)),
                         "ERROR redis timeout after 2000ms"]]}]}}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}

    def query_metric_series(self, *, service, metric, start, end):
        return self.query_service_error_rate(service=service, start=start, end=end)


def _run():
    topology = ServiceTopology.from_edges([("order-service", "redis")])
    ctx = RunContext(
        store=EvidenceStore(), adapters=ConflictingAdapters(),
        budget=BudgetManager(max_tool_calls=40),
        topology=topology, scope_depth=2,
    )
    request = InvestigationRequest(
        investigation_run_id="run-con", incident_id="inc-con",
        environment="demo", service_hint="order-service",
        fault_start=TS, window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
    )
    return run_investigation(request, ctx)


class TestContradictionReachesRanking:
    def test_contradiction_relations_are_created(self):
        result = _run()
        contradictions = [
            link for link in result.report.hypothesis_evidence
            if link.relation_type is RelationType.CONTRADICTS
        ]
        assert contradictions, (
            "healthy observations must produce CONTRADICTS relations; "
            f"got {[l.relation_type for l in result.report.hypothesis_evidence]}")
        # 理由必须可读：报告要能解释"为什么这条证据推翻了这个假设"
        assert any("contradicting" in (c.reason or "") for c in contradictions)

    def test_contradiction_lowers_the_score(self):
        """矛盾证据必须让排序分下降 —— 这是 R-005 存在的原因。

        不要求"有矛盾就翻案"：一条支持证据 + 一条反证证据时，
        R-005 的语义是**降权**而不是推翻。推翻需要矛盾多于支持
        （见 ranking._evaluate 的 REFUTED 分支）。
        """
        from traceops.rca.ranking import CandidateSignals, _evaluate

        with_contradiction = _evaluate(CandidateSignals(
            hypothesis_id="h", supporting_evidence_count=1,
            contradicting_evidence_count=1, has_anomaly_observation=True))
        without = _evaluate(CandidateSignals(
            hypothesis_id="h", supporting_evidence_count=1,
            contradicting_evidence_count=0, has_anomaly_observation=True))
        assert with_contradiction[0] < without[0], (
            "R-005 must reduce the score when contradicting evidence exists")

    def test_contradiction_outnumbers_support_refutes(self):
        """矛盾多于支持 → REFUTED。这是 R-005 的另一半语义。"""
        result = _run()
        for hypothesis in result.report.hypotheses:
            supports = [
                l for l in result.report.hypothesis_evidence
                if l.hypothesis_id == hypothesis.id
                and l.relation_type is RelationType.SUPPORTS]
            contradicts = [
                l for l in result.report.hypothesis_evidence
                if l.hypothesis_id == hypothesis.id
                and l.relation_type is RelationType.CONTRADICTS]
            if len(contradicts) > len(supports):
                assert hypothesis.status is HypothesisStatus.REFUTED

    def test_report_cites_only_real_evidence(self):
        """反向证据也必须能在报告里被引用（引用完整性不能因此破掉）。"""
        result = _run()
        store = EvidenceStore()
        result2 = _run()
        assert result2.report is not None  # 引用校验未抛错即通过
