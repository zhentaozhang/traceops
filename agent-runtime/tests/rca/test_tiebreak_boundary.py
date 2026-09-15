"""排序的**最后一级**兜底：所有实质信号相同时按服务名字母序。

## 为什么这是一个需要被记录的边界，而不是要修的 bug

排序键必须**完全确定**（`ranking.py` 的注释写得很清楚）：UUID 兜底等于
"同分即随机"，同一场景两次运行会给出不同根因，评估指标会随执行顺序漂移。

于是当所有实质信号都相同时 —— 分数一样、拓扑深度一样 —— 只剩字母序可用。
后果是：真实根因如果字母序靠后，就会排到 rank 2。

这不是实现错误，是"确定性 > 偶然正确"的取舍。但它**有代价**，所以必须：

1. 行为被测试钉住（有人改排序键时立刻发现）
2. 代价被指标量出来（F-010 让 AC@1/Avg@5/MRR 不再是满分）

## 边界的确切形状

```
alpha-payment  score=2  depth=1   ← 字母序靠前
zulu-cache     score=2  depth=1   ← 真根因，字母序靠后
                   ↑ 同分同深度 → 只能按名字
```
"""
from __future__ import annotations

from traceops.rca.ranking import CandidateSignals, rank_hypotheses
from traceops.domain.models import RootCauseHypothesis
from traceops.topology.graph import ServiceTopology


def _hyp(service: str) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="r", candidate_service=service,
        fault_type="availability_degradation",
        failure_mechanism=f"{service} degraded", predicted_observations=[],
    )


def _signals(hypothesis, *, supporting: int = 1) -> CandidateSignals:
    return CandidateSignals(
        hypothesis_id=hypothesis.id,
        supporting_evidence_count=supporting,
        has_anomaly_observation=True,
    )


class TestEqualEverythingFallsBackToAlphabetical:
    def test_later_alphabetical_answer_lands_second(self):
        """同分同深度 → 字母序兜底，zulu 排第二。"""
        topology = ServiceTopology.from_edges([
            ("api-gateway", "alpha-payment"),
            ("api-gateway", "zulu-cache"),
        ])
        alpha = _hyp("alpha-payment")
        zulu = _hyp("zulu-cache")
        ranked = rank_hypotheses(
            [alpha, zulu],
            [_signals(alpha), _signals(zulu)],
            topology=topology,
        )
        assert ranked[0].hypothesis_id == alpha.id
        assert ranked[1].hypothesis_id == zulu.id

    def test_earlier_alphabetical_answer_stays_first(self):
        """反向命名 → 同一机制下正确答案排第一。"""
        topology = ServiceTopology.from_edges([
            ("api-gateway", "aaa-cache"),
            ("api-gateway", "zzz-payment"),
        ])
        cache = _hyp("aaa-cache")
        payment = _hyp("zzz-payment")
        ranked = rank_hypotheses(
            [cache, payment], [_signals(cache), _signals(payment)],
            topology=topology,
        )
        assert ranked[0].hypothesis_id == cache.id

    def test_deterministic_across_runs(self):
        """同一输入必须永远得到同一顺序 —— 这是该取舍存在的理由。"""
        topology = ServiceTopology.from_edges([
            ("api-gateway", "alpha-payment"),
            ("api-gateway", "zulu-cache"),
        ])
        orders = []
        for _ in range(5):
            alpha = _hyp("alpha-payment")
            zulu = _hyp("zulu-cache")
            ranked = rank_hypotheses(
                [alpha, zulu], [_signals(alpha), _signals(zulu)], topology=topology)
            by_id = {alpha.id: "alpha", zulu.id: "zulu"}
            orders.append([by_id[r.hypothesis_id] for r in ranked])
        assert orders == [["alpha", "zulu"]] * 5


class TestSubstantiveSignalsStillWin:
    """字母序只是最后一级 —— 任何实质信号都必须能压过它。"""

    def test_rule_hit_beats_alphabetical(self):
        """规则命中（而非证据条数）必须压过字母序。

        注意 R3 决议：评分是**二元特征**（R-001~R-007 的命中与否），
        不做"证据条数加权"。所以这里给 zulu 一个 R-003（慢 span）命中，
        而不是把它支持证据的条数调高 —— 后者按设计不影响分数。
        """
        topology = ServiceTopology.from_edges([
            ("api-gateway", "alpha-payment"),
            ("api-gateway", "zulu-cache"),
        ])
        alpha = _hyp("alpha-payment")
        zulu = _hyp("zulu-cache")
        signals = [
            _signals(alpha),
            CandidateSignals(hypothesis_id=zulu.id, supporting_evidence_count=1,
                            has_anomaly_observation=True, trace_anomaly=True),
        ]
        ranked = rank_hypotheses([alpha, zulu], signals, topology=topology)
        assert ranked[0].hypothesis_id == zulu.id, (
            "a rule hit (R-003) must outrank the alphabetical fallback")

    def test_evidence_count_alone_does_not_change_ranking(self):
        """记录设计：证据**条数**不影响分数（R3 的二元特征决议）。"""
        from traceops.rca.ranking import _evaluate

        scores = {
            n: _evaluate(CandidateSignals(
                hypothesis_id="h", supporting_evidence_count=n,
                has_anomaly_observation=True))[0]
            for n in (1, 3, 10)
        }
        assert set(scores.values()) == {0}, (
            f"evidence count must not be a weighted score, got {scores}")

    def test_deeper_service_beats_alphabetical(self):
        """拓扑深度不同时不再走字母序：越下游越像根因。"""
        topology = ServiceTopology.from_edges([
            ("api-gateway", "alpha-upstream"),
            ("alpha-upstream", "zulu-leaf"),
        ])
        alpha = _hyp("alpha-upstream")
        zulu = _hyp("zulu-leaf")
        ranked = rank_hypotheses(
            [alpha, zulu], [_signals(alpha), _signals(zulu)], topology=topology)
        assert ranked[0].hypothesis_id == zulu.id
