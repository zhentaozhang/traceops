"""Cycle 1 RED: Domain model tests (Evidence / HypothesisEvidence / RootCauseHypothesis).

契约来源: 修订记录 R2 (HypothesisEvidence 与 EvidenceRelation 分离)
         蓝图 §5.3 (Evidence 字段定义, observed_at vs collected_at)
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from traceops.domain.models import (
    Evidence,
    EvidenceType,
    HypothesisEvidence,
    HypothesisStatus,
    RCAReport,
    RelationType,
    RootCauseHypothesis,
)

TS = datetime(2026, 10, 30, 2, 0, 0, tzinfo=UTC)


def make_evidence(**overrides) -> Evidence:
    base = dict(
        incident_id="inc-001",
        investigation_run_id="inv-001",
        evidence_type=EvidenceType.METRIC,
        source_id="prometheus",
        service_id="redis",
        observed_at=TS,
        window_start=TS - timedelta(minutes=5),
        window_end=TS + timedelta(minutes=5),
        collected_at=TS + timedelta(minutes=5),
        query_template_id="redis_pool_active",
        query_params={"service": "redis", "window": "5m"},
        normalized_data={"series": [[1.0], [2.0]]},
    )
    base.update(overrides)
    return Evidence(**base)


class TestEvidence:
    def test_minimal_valid_evidence(self):
        ev = make_evidence()
        assert ev.id  # auto-generated
        assert ev.schema_version  # auto-tagged

    def test_observed_at_before_collected_at(self):
        with pytest.raises(ValidationError, match="collected_at"):
            make_evidence(collected_at=TS - timedelta(minutes=10))

    def test_window_boundaries_around_observed_at(self):
        with pytest.raises(ValidationError, match="window"):
            make_evidence(window_start=TS + timedelta(minutes=1))

    def test_raw_snapshot_ref_implies_checksum(self):
        ev = make_evidence()
        assert ev.checksum == ""
        ev2 = make_evidence(raw_snapshot_ref="s3://evidence/001.json")
        assert ev2.checksum == ""  # set by store at persist time, not by user

    def test_normalized_data_is_dict(self):
        with pytest.raises(ValidationError):
            make_evidence(normalized_data="raw log line text")


class TestHypothesisEvidence:
    def test_supports_link(self):
        link = HypothesisEvidence(
            hypothesis_id="h-001", evidence_id="e-001",
            relation_type=RelationType.SUPPORTS,
            reason="pool active at max", created_by="rule",
        )
        assert link.relation_type == RelationType.SUPPORTS

    def test_created_by_enum(self):
        with pytest.raises(ValidationError):
            HypothesisEvidence(
                hypothesis_id="h-001", evidence_id="e-001",
                relation_type=RelationType.SUPPORTS, created_by="llm",
            )


class TestRootCauseHypothesis:
    def test_status_transitions_enum(self):
        h = RootCauseHypothesis(
            id="h-001", investigation_run_id="inv-001",
            candidate_service="redis", candidate_component="connection-pool",
            fault_type="pool_exhaustion", failure_mechanism="pool saturation",
            predicted_observations=["pool_pending_increased"],
        )
        assert h.status == HypothesisStatus.PROPOSED
        h.status = HypothesisStatus.SUPPORTED
        assert h.status == HypothesisStatus.SUPPORTED

    def test_rejects_evidence_lists(self):
        # R2: evidence links live in HypothesisEvidence, not on the hypothesis
        with pytest.raises(ValidationError):
            RootCauseHypothesis(
                id="h-001", investigation_run_id="inv-001",
                candidate_service="redis", fault_type="x", failure_mechanism="y",
                supporting_evidence_ids=["e-001"],
                predicted_observations=[],
            )


class TestRCAReport:
    def test_report_collects_evidence_ids(self):
        ev1, ev2, ev3 = make_evidence(), make_evidence(), make_evidence()
        h = RootCauseHypothesis(
            id="h-001", investigation_run_id="inv-001",
            candidate_service="redis", fault_type="pool_exhaustion",
            failure_mechanism="pool saturation",
            predicted_observations=[],
        )
        link = HypothesisEvidence(hypothesis_id=h.id, evidence_id=ev2.id,
                                  relation_type=RelationType.SUPPORTS, created_by="rule")
        report = RCAReport(
            investigation_run_id="inv-001",
            diagnosis_status="SUPPORTED",
            summary="Redis connection pool exhaustion",
            hypotheses=[h],
            hypothesis_evidence=[link],
            evidence=[ev1, ev2, ev3],
            missing_data=["redis pool pending count"],
            abstained=False,
        )
        ids = report.all_evidence_ids()
        assert ev1.id in ids and ev2.id in ids and ev3.id in ids

    def test_unsupported_status(self):
        report = RCAReport(
            investigation_run_id="inv-001",
            diagnosis_status="INCONCLUSIVE",
            summary="insufficient evidence",
            hypotheses=[], hypothesis_evidence=[], evidence=[],
            missing_data=["pool metrics", "traces"],
            abstained=True,
        )
        assert report.abstained is True

    def test_invalid_diagnosis_status(self):
        with pytest.raises(ValidationError):
            RCAReport(
                investigation_run_id="inv-001", diagnosis_status="MAYBE",
                summary="", hypotheses=[], hypothesis_evidence=[],
                evidence=[], missing_data=[], abstained=False,
            )
