"""Cycle 3 RED: Evidence Store + 确定性 Validators + 报告引用完整性检查。

blueprint §6.5 + REVISIONS-1.1 R1:
- P0 四个校验器全部确定性实现: Provenance / Scope / Temporal / Reference
- Store 负责持久化时计算 checksum（Evidence.checksum 不由调用方设置）
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import (
    Evidence,
    EvidenceType,
    HypothesisEvidence,
    RCAReport,
    RelationType,
    RootCauseHypothesis,
)
from traceops.evidence.normalizer import normalize
from traceops.evidence.store import EvidenceStore, ValidationResult

TS = datetime(2026, 10, 30, 2, 0, 0, tzinfo=UTC)
WINDOW = (TS - timedelta(minutes=5), TS + timedelta(minutes=5))
INCIDENT_WINDOW = (TS - timedelta(minutes=10), TS + timedelta(minutes=15))


def make_store() -> EvidenceStore:
    return EvidenceStore()


def make_metric_evidence(run_id="inv-001", service="redis") -> Evidence:
    return normalize(
        evidence_type=EvidenceType.METRIC, source_id="prometheus",
        incident_id="inc-001", run_id=run_id, service_id=service,
        query_template_id="redis_pool_active", query_params={"service": service},
        raw_payload={"resultType": "vector", "result": [{"metric": {"service": service}, "value": [1793318400, "50"]}]},
        observed_at=TS, window=WINDOW,
    )


class TestStore:
    def test_add_and_get(self):
        store = make_store()
        ev = make_metric_evidence()
        stored = store.add(ev)
        assert stored.checksum != ""  # checksum computed at persist time
        assert store.get(ev.id) is stored

    def test_checksum_deterministic_per_content(self):
        store = make_store()
        e1 = make_metric_evidence(run_id="inv-A")
        e2 = make_metric_evidence(run_id="inv-B")
        s1, s2 = store.add(e1), store.add(e2)
        # same normalized content → same checksum even though run_id differs
        assert s1.checksum == s2.checksum

    def test_checksum_changes_with_content(self):
        store = make_store()
        e1 = make_metric_evidence()
        e2 = make_metric_evidence()
        e2.normalized_data["series"][0]["value"] = [1793318400, 999.0]
        s1, s2 = store.add(e1), store.add(e2)
        assert s1.checksum != s2.checksum

    def test_get_missing_raises_keyerror(self):
        store = make_store()
        with pytest.raises(KeyError):
            store.get("e-does-not-exist")

    def test_list_by_incident_and_run(self):
        store = make_store()
        a = store.add(make_metric_evidence(run_id="inv-001"))
        b = store.add(make_metric_evidence(run_id="inv-002"))
        run1 = store.list(incident_id="inc-001", run_id="inv-001")
        assert [e.id for e in run1] == [a.id]
        all_inc = store.list(incident_id="inc-001")
        assert {a.id, b.id} <= {e.id for e in all_inc}

    def test_persists_snapshot_record(self):
        store = make_store()
        ev = store.add(make_metric_evidence())
        snap = store.get_snapshot(ev.id)
        assert snap is not None
        assert snap["evidence_id"] == ev.id
        assert snap["raw_payload"]["resultType"] == "vector"


class TestValidators:
    def test_provenance_passes_with_registered_source(self):
        store = make_store()
        store.register_source("prometheus", allowed=True)
        ev = store.add(make_metric_evidence())
        result = store.validate(ev.id)
        assert result.passed and result.checks["provenance"] == "PASS"

    def test_provenance_fails_with_unregistered_source(self):
        store = make_store()
        ev = store.add(make_metric_evidence())  # no register_source call
        result = store.validate(ev.id)
        assert not result.passed
        assert result.checks["provenance"] == "FAIL"

    def test_scope_passes_for_same_incident(self):
        store = make_store()
        ev = store.add(make_metric_evidence())
        result = store.validate(ev.id, incident_id="inc-001")
        assert result.checks["scope"] == "PASS"

    def test_scope_fails_for_other_incident(self):
        store = make_store()
        ev = store.add(make_metric_evidence())
        result = store.validate(ev.id, incident_id="inc-999")
        assert not result.passed and result.checks["scope"] == "FAIL"

    def test_temporal_passes_within_window(self):
        store = make_store()
        ev = store.add(make_metric_evidence())
        result = store.validate(ev.id, investigation_window=INCIDENT_WINDOW)
        assert result.checks["temporal"] == "PASS"

    def test_temporal_fails_outside_window(self):
        store = make_store()
        ev = store.add(make_metric_evidence())
        late_window = (TS + timedelta(hours=1), TS + timedelta(hours=2))
        result = store.validate(ev.id, investigation_window=late_window)
        assert not result.passed and result.checks["temporal"] == "FAIL"

    def test_checksum_tamper_detected(self):
        store = make_store()
        ev = store.add(make_metric_evidence())
        ev.normalized_data["series"][0]["value"] = [1793318400, 1.0]  # tamper after persist
        result = store.validate(ev.id)
        assert not result.passed
        assert result.checks["checksum"] == "FAIL"


class TestReportReferenceValidation:
    def _report(self, store: EvidenceStore, referenced_ids: list[str]) -> RCAReport:
        h = RootCauseHypothesis(
            id="h-001", investigation_run_id="inv-001",
            candidate_service="redis", fault_type="pool_exhaustion",
            failure_mechanism="saturation", predicted_observations=[],
        )
        links = [
            HypothesisEvidence(hypothesis_id="h-001", evidence_id=eid,
                               relation_type=RelationType.SUPPORTS, created_by="rule")
            for eid in referenced_ids
        ]
        return RCAReport(
            investigation_run_id="inv-001", diagnosis_status="SUPPORTED",
            summary="pool exhausted", hypotheses=[h], hypothesis_evidence=links,
            evidence=[], missing_data=[], abstained=False,
        )

    def test_valid_report_passes(self):
        store = make_store()
        ev = store.add(make_metric_evidence())
        report = self._report(store, [ev.id])
        result = store.validate_report_references(report)
        assert result.passed

    def test_missing_reference_fails_with_ids(self):
        store = make_store()
        ev = store.add(make_metric_evidence())
        report = self._report(store, [ev.id, "e-fake-1", "e-fake-2"])
        result = store.validate_report_references(report)
        assert not result.passed
        assert result.invalid_ids == ["e-fake-1", "e-fake-2"]

    def test_report_from_other_incident_rejected(self):
        store = make_store()
        ev = store.add(make_metric_evidence())
        report = self._report(store, [ev.id])
        result = store.validate_report_references(report, incident_id="inc-999")
        assert not result.passed
