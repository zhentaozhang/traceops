"""平局时按**机制与证据的匹配度**排序，而不是按 `fault_type` 字母序。

## 缺陷（实测发现）

同一服务出现两条同分候选时，排序进入 `fault_type` 字母序兜底：

```
zulu-cache/availability_degradation   ← a 在前
zulu-cache/latency_degradation        ← l 在后
```

于是 `availability_degradation` 被排成 rank1，**仅仅因为字母顺序**。
问题在于它手里只有错误率间接支持，而 `latency_degradation` 有一条
**1.8 秒的慢 span 直接支持**（`slow_span` 正是它的期望观测）。

这是"任意选择被当成结论"，而 ARCHITECTURE 早就把这个字母序兜底
记为已知成本 —— 现在可以把它换成**有依据**的判据。

## 为什么这不是"为模型作弊"

判据对**所有来源**一致：规则、模型、人工创建的候选都走同一条排序。
改进的是**排序依据本身**，不是"模型来的假设加分"。
如果规则提出了一个机制与证据高度匹配的候选，它同样受益。

## 判据

`mechanism_evidence_fit` = 该候选的支持证据中，有多少条命中它**自己**的
`expected_observations`（即 `semantic.EXPECTATIONS` 里登记的 marker）。

这已经成为语义闸门的输入，复用它可以避免引入第二份"期望观测"定义。

排序位置：`score` 之后、`blame_order` 之前。
理由：机制自洽是比"离根因多远"更硬的证据质量判据 ——
一个机制明显不符的候选，即便更靠近下游也不该排前。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import (
    Evidence, EvidenceType, HypothesisEvidence, RelationType, RootCauseHypothesis,
)
from traceops.evidence.store import EvidenceStore
from traceops.rca.ranking import CandidateSignals, derive_signals, rank_hypotheses


TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _slow_span_evidence(service: str = "zulu-cache") -> Evidence:
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.TRACE,
        source_id="tempo", service_id=service, observed_at=TS,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id="qt", query_params={},
        normalized_data={"kind": "trace", "total_records": 1, "spans": [{
            "trace_id": "t", "span_id": "s", "service": service,
            "operation_name": "zulu-cache.get", "duration_us": 1_800_000, "error": False}]},
        checksum="")


def _metric_evidence(service: str = "zulu-cache") -> Evidence:
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.METRIC,
        source_id="prom", service_id=service, observed_at=TS,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id="qt", query_params={},
        normalized_data={"kind": "metric", "result_type": "matrix", "result_count": 1,
                         "total_records": 1,
                         "series": [{"metric": {}, "values": [[TS.timestamp(), "0.11"]]}]},
        checksum="")


def _link(hid: str, eid: str) -> HypothesisEvidence:
    return HypothesisEvidence(hypothesis_id=hid, evidence_id=eid,
                              relation_type=RelationType.SUPPORTS,
                              reason="t", created_by="rule")


class TestMechanismFitBreaksTies:
    def test_mechanism_that_matches_its_evidence_wins_the_tie(self):
        """latency 假设有 slow_span 直接支持，availability 只有指标 → latency 应排前。"""
        latency = RootCauseHypothesis(
            investigation_run_id="r", candidate_service="zulu-cache",
            fault_type="latency_degradation", failure_mechanism="slow",
            predicted_observations=["slow_span"])
        availability = RootCauseHypothesis(
            investigation_run_id="r", candidate_service="zulu-cache",
            fault_type="availability_degradation", failure_mechanism="errors",
            predicted_observations=["error_rate_rise", "error_logs"])

        slow, metric = _slow_span_evidence(), _metric_evidence()
        store = EvidenceStore()
        store.add(slow)
        store.add(metric)
        links = [_link(latency.id, slow.id), _link(availability.id, metric.id)]

        signals = [
            derive_signals(latency, links, store, fault_start=TS),
            derive_signals(availability, links, store, fault_start=TS),
        ]
        ranked = rank_hypotheses([latency, availability], signals)
        by_id = {r.hypothesis_id: r for r in ranked}
        assert by_id[latency.id].rank == 1, (
            "the hypothesis whose mechanism matches a direct observation must win "
            f"(got {by_id[latency.id].rank}; alphabetical order would pick availability)")

    def test_alphabetical_order_is_not_the_decider(self):
        """反向验证：机制匹配的是字母序靠后的那个，仍然应该赢。"""
        # 把机制名字换到字母序更靠后的位置：latency > availability
        latency = RootCauseHypothesis(
            investigation_run_id="r", candidate_service="zulu-cache",
            fault_type="latency_degradation", failure_mechanism="slow",
            predicted_observations=["slow_span"])
        availability = RootCauseHypothesis(
            investigation_run_id="r", candidate_service="zulu-cache",
            fault_type="availability_degradation", failure_mechanism="errors",
            predicted_observations=["error_rate_rise"])

        slow, metric = _slow_span_evidence(), _metric_evidence()
        store = EvidenceStore()
        store.add(slow)
        store.add(metric)
        links = [_link(latency.id, slow.id), _link(availability.id, metric.id)]

        signals = [
            derive_signals(availability, links, store, fault_start=TS),
            derive_signals(latency, links, store, fault_start=TS),
        ]
        ranked = rank_hypotheses([availability, latency], signals)
        # 传参顺序也换过，确保不是靠输入顺序
        assert ranked[0].hypothesis_id == latency.id

    def test_both_matching_still_falls_back_deterministically(self):
        """两条都匹配时仍需确定性兜底 —— 不能引入随机。"""
        a = RootCauseHypothesis(
            investigation_run_id="r", candidate_service="svc-a",
            fault_type="latency_degradation", failure_mechanism="m",
            predicted_observations=["slow_span"])
        b = RootCauseHypothesis(
            investigation_run_id="r", candidate_service="svc-b",
            fault_type="latency_degradation", failure_mechanism="m",
            predicted_observations=["slow_span"])
        slow_a, slow_b = _slow_span_evidence("svc-a"), _slow_span_evidence("svc-b")
        store = EvidenceStore()
        store.add(slow_a)
        store.add(slow_b)
        links = [_link(a.id, slow_a.id), _link(b.id, slow_b.id)]

        sig = [derive_signals(a, links, store, fault_start=TS),
               derive_signals(b, links, store, fault_start=TS)]
        first = rank_hypotheses([a, b], sig)
        second = rank_hypotheses([b, a], sig)
        assert first[0].hypothesis_id == second[0].hypothesis_id, (
            "identical inputs must rank identically regardless of input order")
