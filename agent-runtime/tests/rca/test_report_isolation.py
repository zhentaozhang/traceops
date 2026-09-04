"""评测隔离：报告只能引用**本次调查**的证据。

## 为什么需要（aurora-lab `runner/run.py:157 isolation_check()`）

aurora-lab 的做法是：每个 (scenario, repeat) 新建 user + org，并检查首轮
`list_incidents` 里有没有别的 run 的 incident id，防止跨场景记忆泄漏。

TraceOps 的对应风险不在"记忆"，而在**引用校验口径**：

`build_report` 调用 `store.validate_report_references(report)` 时**没传
`incident_id`**，于是校验只查"这个证据 ID 在 Store 里存在不存在"。
同一个 Store 里如果混进别的场景的证据（评测时尤其容易，
比如复用 store 或写入未清理），一个"引用了他处证据"的报告会被判定为
**引用完整**——因为 ID 确实存在。

这就是"引用完整性通过了，但证据是别人的"。

## 正确行为

校验必须绑定本次调查的身份。Store 已支持 `incident_id` 参数，
缺口只在调用方没传。
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


def _evidence(incident_id: str, run_id: str = "run-1") -> Evidence:
    return Evidence(
        incident_id=incident_id,
        investigation_run_id=run_id,
        evidence_type=EvidenceType.LOG,
        source_id="loki",
        service_id="redis",
        observed_at=TS,
        window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
        query_template_id="qt-service-logs-v1",
        query_params={},
        normalized_data={"kind": "log", "total_records": 1, "records": [
            {"timestamp": TS.isoformat(), "message": "redis timeout", "labels": {"level": "error"}}]},
        checksum="",
    )


class TestCrossIncidentReferencesRejected:
    def test_report_citing_another_incidents_evidence_is_rejected(self):
        """报告引用别的事故的证据 → 必须抛 ReportIntegrityError，不能算通过。"""
        hypothesis = RootCauseHypothesis(
            investigation_run_id="run-1", candidate_service="redis",
            fault_type="pool_exhaustion", failure_mechanism="pool exhausted",
            predicted_observations=[], status=HypothesisStatus.SUPPORTED,
        )
        store = EvidenceStore()
        foreign = _evidence(incident_id="inc-OTHER")
        store.add(foreign)

        from traceops.domain.models import HypothesisEvidence
        from traceops.rca.ranking import RankedCandidate

        with pytest.raises(Exception) as exc:
            build_report(
                investigation_run_id="run-1",
                hypotheses=[hypothesis],
                links=[HypothesisEvidence(
                    hypothesis_id=hypothesis.id, evidence_id=foreign.id,
                    relation_type="SUPPORTS", created_by="rule")],
                evidence=[foreign],
                ranked=[RankedCandidate(hypothesis_id=hypothesis.id, score=0, rank=1,
                                       status=HypothesisStatus.SUPPORTED)],
                fault_start=TS,
                store=store,
                incident_id="inc-MINE",
            )
        assert "incident" in str(exc.value).lower() or "integrity" in str(exc.value).lower()

    def test_same_incident_evidence_passes(self):
        hypothesis = RootCauseHypothesis(
            investigation_run_id="run-1", candidate_service="redis",
            fault_type="pool_exhaustion", failure_mechanism="pool exhausted",
            predicted_observations=[], status=HypothesisStatus.SUPPORTED,
        )
        store = EvidenceStore()
        mine = _evidence(incident_id="inc-MINE")
        store.add(mine)

        from traceops.domain.models import HypothesisEvidence
        from traceops.rca.ranking import RankedCandidate

        report = build_report(
            investigation_run_id="run-1",
            hypotheses=[hypothesis],
            links=[HypothesisEvidence(
                hypothesis_id=hypothesis.id, evidence_id=mine.id,
                relation_type="SUPPORTS", created_by="rule")],
            evidence=[mine],
            ranked=[RankedCandidate(hypothesis_id=hypothesis.id, score=0, rank=1,
                                   status=HypothesisStatus.SUPPORTED)],
            fault_start=TS,
            store=store,
            incident_id="inc-MINE",
        )
        assert report.diagnosis_status == "SUPPORTED"
