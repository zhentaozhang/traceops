"""多个**独立**故障应当报出多个根因，而不是判成"歧义"。

## 这是 PRD 里一个未决的架构问题

`report.py` 的歧义闸门：多个候选在不同服务之间同分 → INCONCLUSIVE。
注释写得很清楚，它的目的是防"任选一个当根因"的过度断言：

```
Ambiguous evidence: N candidate root causes across M services are equally
supported; no rule discriminates them
```

这在「**一个**故障、多个候选」时是对的。但它在「**两个**故障同时发生」时
是错的：那不是"分不清哪个是根因"，而是"两个都是根因"。

aurora-lab 场景 12 就是这种形态，它的 rubric 有两条 must_identify，
正确答案是两条独立根因。REPORT.md 原话：
「A real second outage went unreported, and the report said it was not there」。

## 判据：拓扑上互不相干

同分不等于歧义。区别在于：

| 情况 | 两个候选的关系 | 正确输出 |
|---|---|---|
| 一个故障、多个可能的解释 | 拓扑上**相干**（一个依赖另一个 / 共享依赖） | INCONCLUSIVE（真歧义） |
| 两个独立故障 | 拓扑上**互不相干** | 两条 SUPPORTED |

"相干"的操作定义：任一方能沿依赖/调用方向到达另一方，或共享同一依赖
（兄弟）。互不相干 = 两个故障彼此无法解释对方。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import HypothesisStatus, RootCauseHypothesis
from traceops.rca.ranking import CandidateSignals, RankedCandidate
from traceops.rca.report import build_report
from traceops.evidence.store import EvidenceStore
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _hyp(service: str, fault_type: str) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="r", candidate_service=service, fault_type=fault_type,
        failure_mechanism=f"{service} {fault_type}", predicted_observations=[],
    )


def _report(hypotheses, topology, *, scores=None):
    scores = scores or {}
    ranked = [
        RankedCandidate(hypothesis_id=h.id, score=scores.get(h.id, 0), rank=i,
                        status=HypothesisStatus.SUPPORTED)
        for i, h in enumerate(hypotheses, start=1)
    ]
    return build_report(
        investigation_run_id="r", hypotheses=hypotheses, links=[],
        evidence=[], ranked=ranked, fault_start=TS, store=EvidenceStore(),
        topology=topology,
    )


class TestIndependentFaultsAreBothReported:
    def test_two_unrelated_services_are_two_root_causes(self):
        """redis 与 mysql 互不相干 → 两条根因，不是"歧义"。"""
        topology = ServiceTopology.from_edges([("order-service", "redis")])
        redis = _hyp("redis", "pool_exhaustion")
        mysql = _hyp("mysql", "availability_degradation")

        report = _report([redis, mysql], topology)

        assert report.diagnosis_status == "SUPPORTED", (
            "two independent faults are not an ambiguity; "
            f"got {report.diagnosis_status}: {report.summary}")

    def test_both_services_appear_in_candidates(self):
        topology = ServiceTopology.from_edges([("order-service", "redis")])
        redis = _hyp("redis", "pool_exhaustion")
        mysql = _hyp("mysql", "availability_degradation")
        report = _report([redis, mysql], topology)
        asserted = {h.candidate_service for h in report.hypotheses
                    if h.status is HypothesisStatus.SUPPORTED}
        assert asserted == {"redis", "mysql"}, f"got {asserted}"


class TestRelatedCandidatesStayAmbiguous:
    def test_dependent_services_remain_ambiguous(self):
        """一个依赖另一个、同分 → 仍然判歧义（真正的"分不清"）。"""
        topology = ServiceTopology.from_edges([("order-service", "redis")])
        redis = _hyp("redis", "pool_exhaustion")
        order = _hyp("order-service", "availability_degradation")

        report = _report([redis, order], topology)
        assert report.diagnosis_status == "INCONCLUSIVE"

    def test_siblings_remain_ambiguous(self):
        """共享同一依赖（兄弟）+ 同分 → 相干，仍判歧义。"""
        topology = ServiceTopology.from_edges([
            ("order-service", "redis"), ("inventory-service", "redis")])
        order = _hyp("order-service", "availability_degradation")
        inventory = _hyp("inventory-service", "availability_degradation")

        report = _report([order, inventory], topology)
        assert report.diagnosis_status == "INCONCLUSIVE"

    def test_no_topology_keeps_old_behavior(self):
        """没有拓扑时不猜"相干不相干"，保持旧的歧义闸门行为。"""
        redis = _hyp("redis", "pool_exhaustion")
        mysql = _hyp("mysql", "availability_degradation")
        report = _report([redis, mysql], None)
        assert report.diagnosis_status == "INCONCLUSIVE"


class TestScoresStillMatter:
    def test_clear_winner_is_not_affected(self):
        """分差不明显时直接取最高分，与多根因逻辑无关。"""
        topology = ServiceTopology.from_edges([("order-service", "redis")])
        redis = _hyp("redis", "pool_exhaustion")
        mysql = _hyp("mysql", "availability_degradation")
        report = _report([redis, mysql], topology, scores={redis.id: 3, mysql.id: 0})
        assert report.diagnosis_status == "SUPPORTED"
