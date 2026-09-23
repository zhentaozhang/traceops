"""支持/矛盾的计数必须**分母一致**：都只算真实存在的证据。

## 缺陷（排查静默降级时发现）

`_signals_for` 里两个计数用的规则不同：

```python
# 支持：查不到证据就 continue（只统计真实存在的）
ev = store.get(link.evidence_id)   # KeyError -> continue
supports = [... l.evidence_id in by_id ...]

# 矛盾：**只看 link，完全不查证据是否存在**
contradicts = [l for l in links if l.relation_type == CONTRADICTS]
```

于是引用了**不存在证据**的 CONTRADICTS link 会被算进矛盾计数，而它的支持对应物
却会被跳过。后果：

- `REFUTED` 的判据是「矛盾数 > 支持数」
- 一条脏 link 就能把假设判成 REFUTED，**而它引用的证据根本不在库里**
- 这是"证据驱动"原则的反面：结论来自一条无法核验的引用

这个缺陷在正常路径下不触发（link 都由本进程创建、证据都在），
但它是一条**可以被错误数据击穿**的路径 —— 而 `report.py` 的引用完整性闸门
只校验报告最终引用的证据，管不到"计数阶段"。

## 正确行为

矛盾计数也必须只统计**证据确实存在**的 link。分母一致，
否则任何"比较两个计数"的判据都不可靠。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import (
    Evidence, EvidenceType, HypothesisEvidence, RelationType, RootCauseHypothesis,
)
from traceops.evidence.store import EvidenceStore
from traceops.rca.ranking import derive_signals


TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _hypothesis() -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="r", candidate_service="redis",
        fault_type="availability_degradation",
        failure_mechanism="m", predicted_observations=["error_logs"])


def _log(service: str = "redis") -> Evidence:
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.LOG,
        source_id="loki", service_id=service, observed_at=TS,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id="qt", query_params={},
        normalized_data={"kind": "log", "total_records": 1,
                         "records": [{"timestamp": TS.isoformat(),
                                      "message": "ERROR timeout", "labels": {}}]},
        checksum="")


def _link(hypothesis_id: str, evidence_id: str, relation: RelationType) -> HypothesisEvidence:
    return HypothesisEvidence(hypothesis_id=hypothesis_id, evidence_id=evidence_id,
                              relation_type=relation, reason="test", created_by="rule")


class TestContradictionCountOnlyCountsRealEvidence:
    def test_contradiction_for_missing_evidence_is_not_counted(self):
        """引用了不存在证据的 CONTRADICTS 不该进矛盾计数。

        否则一条脏 link 就能把假设判成 REFUTED —— 而它引用的证据不在库里。
        """
        hypothesis = _hypothesis()
        log = _log()
        store = EvidenceStore()
        store.add(log)
        links = [
            _link(hypothesis.id, log.id, RelationType.SUPPORTS),
            # 这条引用的证据从未入库
            _link(hypothesis.id, "e-does-not-exist", RelationType.CONTRADICTS),
        ]
        signals = derive_signals(hypothesis, links, store, fault_start=TS)
        assert signals.contradicting_evidence_count == 0, (
            "a contradiction pointing at non-existent evidence must not be counted")

    def test_real_contradiction_is_still_counted(self):
        """正常矛盾不受影响 —— 证据存在时照常计入。"""
        hypothesis = _hypothesis()
        log = _log()
        store = EvidenceStore()
        store.add(log)
        links = [
            _link(hypothesis.id, log.id, RelationType.SUPPORTS),
            _link(hypothesis.id, log.id, RelationType.CONTRADICTS),
        ]
        signals = derive_signals(hypothesis, links, store, fault_start=TS)
        assert signals.contradicting_evidence_count == 1

    def test_supports_and_contradicts_use_the_same_denominator(self):
        """两个计数的分母必须一致：都基于"证据存在于库"。

        这条是判据可靠性的根基：`REFUTED` 比较这两个数，
        分母不一致时任何比较都无意义。
        """
        hypothesis = _hypothesis()
        log = _log()
        store = EvidenceStore()
        store.add(log)
        links = [
            _link(hypothesis.id, log.id, RelationType.SUPPORTS),
            _link(hypothesis.id, "e-ghost-1", RelationType.CONTRADICTS),
            _link(hypothesis.id, "e-ghost-2", RelationType.CONTRADICTS),
        ]
        signals = derive_signals(hypothesis, links, store, fault_start=TS)
        # 支持=1（真实），矛盾=0（两条都是幽灵证据）
        assert signals.supporting_evidence_count == 1
        assert signals.contradicting_evidence_count == 0
