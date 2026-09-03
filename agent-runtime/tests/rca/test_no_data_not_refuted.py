"""数据源全挂时**不能**输出 REFUTED。

## 为什么

`REFUTED` 的语义是"我查过了，证据推翻了所有候选"。
而"数据源全挂"的真实语义是"我什么都没看见"。

两者在 `build_report` 里现在可能塌缩成同一个结论：只要所有候选都是
`REFUTED` 状态，就输出 `diagnosis = "REFUTED"`。但候选变成 REFUTED 的
条件之一是「冲突证据多于支持证据」——在没有数据时，这个计数是 0 vs 0，
不该产生 REFUTED。

aurora-lab 的教训（`summarization.py` 注释原话）：
「without agent_reasoning the summarizer only sees raw tool outputs and
routinely turns **absence of evidence into fabricated findings**」。
把 absence 当成阴性证据，就是这条。

## 正确行为

- 有证据、且证据确实推翻候选 → `REFUTED`（合法，这是真实的否定结论）
- 没有可用的观测（`missing_data` 表明采集失败/空结果）→ `INCONCLUSIVE`
  并在摘要里说清"缺什么"，**不能**伪装成"已排除"
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import (
    Evidence, EvidenceType, HypothesisStatus, RootCauseHypothesis,
)
from traceops.evidence.store import EvidenceStore
from traceops.rca.report import build_report

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _refuted_hypothesis() -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="run-1",
        candidate_service="redis",
        fault_type="pool_exhaustion",
        failure_mechanism="pool exhausted",
        predicted_observations=[],
        status=HypothesisStatus.REFUTED,
    )


def _ranked(status: HypothesisStatus, hid: str):
    from traceops.rca.ranking import RankedCandidate

    return RankedCandidate(hypothesis_id=hid, score=0, rank=1, status=status)


class TestNoDataCannotProduceRefuted:
    def test_all_refuted_with_failed_collection_is_inconclusive(self):
        """采集中途失败（missing_data 有记录）→ 只能是 INCONCLUSIVE。"""
        hypothesis = _refuted_hypothesis()
        store = EvidenceStore()
        report = build_report(
            investigation_run_id="run-1",
            hypotheses=[hypothesis],
            links=[],
            evidence=[],
            ranked=[_ranked(HypothesisStatus.REFUTED, hypothesis.id)],
            fault_start=TS,
            store=store,
            missing_data=["query_service_logs: datasource unreachable",
                          "query_service_traces: no data"],
        )
        assert report.diagnosis_status == "INCONCLUSIVE", (
            "failed collection must not be reported as a refutation")
        assert "unreachable" in report.summary or "Insufficient" in report.summary

    def test_real_refutation_with_evidence_stays_refuted(self):
        """真的有观测、且证据推翻候选 → REFUTED 是合法结论，不能被削弱。"""
        hypothesis = _refuted_hypothesis()
        store = EvidenceStore()
        contradicted = Evidence(
            incident_id="inc-1",
            investigation_run_id="run-1",
            evidence_type=EvidenceType.LOG,
            source_id="loki",
            service_id="redis",
            observed_at=TS,
            window_start=TS - timedelta(minutes=2),
            window_end=TS + timedelta(minutes=3),
            query_template_id="qt-service-logs-v1",
            query_params={},
            normalized_data={"kind": "log", "total_records": 3, "records": [
                {"timestamp": TS.isoformat(), "message": "redis healthy, low latency",
                 "labels": {"level": "info"}}]},
            checksum="",
        )
        store.add(contradicted)
        report = build_report(
            investigation_run_id="run-1",
            hypotheses=[hypothesis],
            links=[],
            evidence=[contradicted],
            ranked=[_ranked(HypothesisStatus.REFUTED, hypothesis.id)],
            fault_start=TS,
            store=store,
            missing_data=[],
        )
        assert report.diagnosis_status == "REFUTED"
