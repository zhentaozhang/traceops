"""影子模式：闸门照常计算，但不改变结论。

## 为什么需要

三个弃权闸门（歧义 / 语义覆盖率为 0 / 采集不全时的反驳）都是**静默降级**：
结论从 SUPPORTED 变成 INCONCLUSIVE，报告里只留一句人类可读的摘要。

后果是两条实际困难：

1. **无法测量**：想知道"语义闸门到底拦下了多少次本来能给出的结论"，
   只能数 `diagnosis_status` 的分布 —— 而那个分布已经是被闸门改写过的，
   拦下了多少次**根本无从得知**。
2. **无法灰度**：想调整某个闸门的松紧，唯一办法是改代码全量重跑。
   没有"先观察一段时间、看它要拦几次、再决定要不要真的拦"的中间档。

super-agent 的解法是 `AcceptMode = off | observe | auto`：observe 下系统照常
判断、照常记 `would_accept`，但不执行那个动作。搬过来就是：闸门照常算，
把"本该判成什么、因为什么"写进 `shadow_decisions`，真实结论保持不动。

## 与消融开关的区别

`enable_semantic_gate=False` 是"不启用、也不观测"（用于消融实验，E4 臂）。
影子模式是"观测但不执行"（用于线上校准）。前者丢掉信息换取对比基线，
后者同时拿到两个结论 —— 线上只有一次机会，关掉闸门跑一周等于一周没有保护。
"""
from __future__ import annotations

from datetime import UTC, datetime

from traceops.domain.models import HypothesisStatus, RootCauseHypothesis
from traceops.evidence.store import EvidenceStore
from traceops.rca.ranking import RankedCandidate
from traceops.rca.report import build_report
from traceops.rca.semantic import SemanticCheck
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _hyp(service: str, fault_type: str = "pool_exhaustion") -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="r", candidate_service=service, fault_type=fault_type,
        failure_mechanism=f"{service} {fault_type}", predicted_observations=[],
    )


def _report(hypotheses, *, scores=None, topology=None, semantic_checks=None,
            missing_data=None, shadow_gates=None):
    scores = scores or {}
    ranked = [
        RankedCandidate(hypothesis_id=h.id, score=scores.get(h.id, 0), rank=i,
                        status=HypothesisStatus.SUPPORTED)
        for i, h in enumerate(hypotheses, start=1)
    ]
    return build_report(
        investigation_run_id="r", hypotheses=hypotheses, links=[],
        evidence=[], ranked=ranked, fault_start=TS, store=EvidenceStore(),
        topology=topology, semantic_checks=semantic_checks,
        missing_data=missing_data, shadow_gates=shadow_gates,
    )


TWO_RELATED = ServiceTopology.from_edges([("order-service", "redis")])


class TestAmbiguityGate:
    def _ambiguous(self):
        return [_hyp("redis"), _hyp("order-service", "availability_degradation")]

    def test_without_shadow_the_gate_still_abstains(self):
        report = _report(self._ambiguous(), topology=TWO_RELATED)
        assert report.diagnosis_status == "INCONCLUSIVE"
        assert report.shadow_decisions == [], "闸门生效时不该产生影子记录"

    def test_shadowed_gate_keeps_the_conclusion(self):
        report = _report(self._ambiguous(), topology=TWO_RELATED,
                         shadow_gates={"ambiguity_gate"})
        assert report.diagnosis_status == "SUPPORTED", (
            "影子模式下闸门不改变结论，实际 " + report.diagnosis_status)

    def test_shadowed_gate_records_what_it_would_have_decided(self):
        report = _report(self._ambiguous(), topology=TWO_RELATED,
                         shadow_gates={"ambiguity_gate"})
        assert len(report.shadow_decisions) == 1
        decision = report.shadow_decisions[0]
        assert decision["gate"] == "ambiguity_gate"
        assert decision["would_be"] == "INCONCLUSIVE"
        assert decision["reason"] == "ambiguous_tie"
        assert "redis" in decision["detail"]


class TestSemanticGate:
    def _low_coverage(self):
        hyp = _hyp("redis", "latency_degradation")
        checks = {hyp.id: SemanticCheck(
            hypothesis_id=hyp.id,
            expected=("slow_span", "no_error_log"),
            observed=(),
            missing=("slow_span", "no_error_log"),
        )}
        return [hyp], checks

    def test_without_shadow_coverage_zero_abstains(self):
        hyps, checks = self._low_coverage()
        report = _report(hyps, semantic_checks=checks)
        assert report.diagnosis_status == "INCONCLUSIVE"

    def test_shadowed_semantic_gate_keeps_supported(self):
        hyps, checks = self._low_coverage()
        report = _report(hyps, semantic_checks=checks,
                         shadow_gates={"semantic_gate"})
        assert report.diagnosis_status == "SUPPORTED"
        assert [d["gate"] for d in report.shadow_decisions] == ["semantic_gate"]
        assert report.shadow_decisions[0]["reason"] == "semantic_coverage_zero"
        assert "slow_span" in report.shadow_decisions[0]["detail"]


class TestRefutationGate:
    def _refuted(self):
        hyp = _hyp("redis")
        return [hyp], [
            RankedCandidate(hypothesis_id=hyp.id, score=-5, rank=1,
                            status=HypothesisStatus.REFUTED)
        ]

    def _build(self, shadow_gates):
        hyps, ranked = self._refuted()
        return build_report(
            investigation_run_id="r", hypotheses=hyps, links=[], evidence=[],
            ranked=ranked, fault_start=TS, store=EvidenceStore(),
            missing_data=["loki: connection refused"],
            shadow_gates=shadow_gates,
        )

    def test_without_shadow_incomplete_collection_blocks_refutation(self):
        report = self._build(None)
        assert report.diagnosis_status == "INCONCLUSIVE"

    def test_shadowed_refutation_gate_keeps_refuted(self):
        report = self._build({"refutation_gate"})
        assert report.diagnosis_status == "REFUTED"
        assert report.shadow_decisions[0]["reason"] == "refuted_but_incomplete"


class TestShadowIsPerGate:
    def test_unlisted_gates_still_apply(self):
        """只影子一个闸门时，其它闸门必须照常生效 —— 否则就成了全局关闭。"""
        hyps, checks = TestSemanticGate()._low_coverage()
        report = _report(hyps, semantic_checks=checks,
                         shadow_gates={"ambiguity_gate"})
        assert report.diagnosis_status == "INCONCLUSIVE", (
            "语义闸门没被影子化，必须仍然降级")
        assert report.shadow_decisions == []

    def test_no_shadow_decisions_when_no_gate_would_fire(self):
        report = _report([_hyp("redis")], topology=TWO_RELATED,
                         shadow_gates={"ambiguity_gate", "semantic_gate",
                                       "refutation_gate"})
        assert report.diagnosis_status == "SUPPORTED"
        assert report.shadow_decisions == [], (
            "没有闸门要触发时不该留下空影子记录")

    def test_unknown_gate_name_is_ignored(self):
        report = _report([_hyp("redis")], shadow_gates={"no_such_gate"})
        assert report.diagnosis_status == "SUPPORTED"
        assert report.shadow_decisions == []
