"""症状服务不应被断言为「根因」，即使它自己的指标确实异常。

## 这个缺陷是怎么被发现的

给 F-001/F-003 加上 `wrong_answers: ["order-service"]` 后，
`wrong_answer_rate` 立刻变成 0.4 —— 报告把**上报者**当成候选根因：

```
F-001 rank1: redis/pool_exhaustion       status=supported   ← 正确
      rank3: order-service/availability  status=supported   ← 错，但真的存在
F-005 rank5: order-service/availability  status=supported   ← 同一问题
```

这正是 aurora-lab E-1「找到告警的那个故障，就把其余部分宣布为健康」的镜像：
我们是反过来——**把被波及的服务也宣布成根因**。

## 为什么会这样

`_evaluate` 的状态分类只看「支持证据条数 > 0」：

```python
elif signals.supporting_evidence_count > 0:
    status = HypothesisStatus.SUPPORTED
```

而 `order-service` 的错误率确实升高（它是**症状**，本来就该升高）。
症状证据既支持"order-service 有事"，却不构成"order-service 是根因"。

## 正确规则（R-008 症状现行性 / 拓扑方向）

一个候选只有在**它自己不是下游症状、且故障机制对得上**时才是根因候选。
确定性判据：候选服务若在拓扑上**依赖基础候选（rank 1 且分更高）**，
它的异常可以完全由上游根因解释 → 降级为「受影响」而不是「根因」。
"""
from __future__ import annotations

import pytest

from traceops.domain.models import HypothesisStatus, RootCauseHypothesis
from traceops.rca.ranking import CandidateSignals, rank_hypotheses
from traceops.topology.graph import ServiceTopology


def _hypothesis(service: str, fault_type: str = "availability_degradation"):
    return RootCauseHypothesis(
        investigation_run_id="run-1",
        candidate_service=service,
        fault_type=fault_type,
        failure_mechanism=f"{service} degraded",
        predicted_observations=[],
    )


def _by_service(ranked, hypotheses):
    """RankedCandidate 只有 hypothesis_id；测试需要一个服务名索引。"""
    by_id = {h.id: h for h in hypotheses}
    return {by_id[r.hypothesis_id].candidate_service: r for r in ranked}


def _signals_for(hypothesis, supporting: int = 1, contradicting: int = 0):
    """按真实签名构造：CandidateSignals 必须带 hypothesis_id。"""
    return _signals(supporting, contradicting, hypothesis.id)


def _signals(supporting: int = 1, contradicting: int = 0, hypothesis_id: str = "h-x"):
    return CandidateSignals(
        hypothesis_id=hypothesis_id,
        supporting_evidence_count=supporting,
        contradicting_evidence_count=contradicting,
        has_anomaly_observation=True,
    )


class TestDownstreamSymptomIsNotARootCause:
    def test_service_that_depends_on_the_root_is_not_supported(self):
        """order-service 依赖 redis，且 redis 分数更高 → order-service 只是受影响。"""
        topology = ServiceTopology.from_edges([("order-service", "redis")])
        redis = _hypothesis("redis", "pool_exhaustion")
        order = _hypothesis("order-service", "availability_degradation")

        ranked = rank_hypotheses(
            [redis, order],
            signals=[_signals_for(redis, 2), _signals_for(order, 1)],
            topology=topology,
        )

        by_service = _by_service(ranked, [redis, order])
        assert by_service["redis"].status is HypothesisStatus.SUPPORTED
        assert by_service["order-service"].status is not HypothesisStatus.SUPPORTED, (
            "downstream symptom must not be asserted as a root cause")

    def test_ranking_still_orders_root_first(self):
        topology = ServiceTopology.from_edges([("order-service", "redis")])
        redis = _hypothesis("redis", "pool_exhaustion")
        order = _hypothesis("order-service", "availability_degradation")

        ranked = rank_hypotheses(
            [redis, order],
            signals=[_signals_for(redis, 2), _signals_for(order, 1)],
            topology=topology,
        )
        assert _by_service(ranked, [redis, order]).keys() and ranked[0].hypothesis_id == redis.id


class TestIndependentServiceIsStillARootCause:
    def test_unrelated_service_with_evidence_is_not_downgraded(self):
        """拓扑上互不相干的两个服务同时故障 → 两个都可以是根因（多故障）。"""
        topology = ServiceTopology.from_edges([("order-service", "redis")])
        redis = _hypothesis("redis", "pool_exhaustion")
        billing = _hypothesis("billing-api", "dependency_timeout")

        ranked = rank_hypotheses(
            [redis, billing],
            signals=[_signals_for(redis, 2), _signals_for(billing, 2)],
            topology=topology,
        )
        by_service = _by_service(ranked, [redis, billing])
        assert by_service["billing-api"].status is HypothesisStatus.SUPPORTED, (
            "an unrelated service with its own evidence must stay a candidate")

    def test_no_topology_means_no_downgrade(self):
        """没有拓扑时不能瞎猜方向：宁可不降级，也不要误杀真实根因。"""
        redis = _hypothesis("redis", "pool_exhaustion")
        order = _hypothesis("order-service", "availability_degradation")

        ranked = rank_hypotheses(
            [redis, order],
            signals=[_signals_for(redis, 2), _signals_for(order, 1)],
            topology=None,
        )
        by_service = _by_service(ranked, [redis, order])
        assert by_service["order-service"].status is HypothesisStatus.SUPPORTED


class TestDowngradeIsExplained:
    def test_downgraded_hypothesis_records_the_reason(self):
        """降级必须可解释：报告里要能看出"为什么它不是根因"。"""
        topology = ServiceTopology.from_edges([("order-service", "redis")])
        redis = _hypothesis("redis", "pool_exhaustion")
        order = _hypothesis("order-service", "availability_degradation")

        ranked = rank_hypotheses(
            [redis, order],
            signals=[_signals_for(redis, 2), _signals_for(order, 1)],
            topology=topology,
        )
        order_ranked = _by_service(ranked, [redis, order])["order-service"]
        assert any("R-008" in rule for rule in order_ranked.applied_rules), (
            f"expected R-008 among rules, got {order_ranked.applied_rules}")
