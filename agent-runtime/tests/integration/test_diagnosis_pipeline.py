"""Cycle 7 RED: 端到端确定性诊断管线集成测试（无 LLM）。

链路: 多源证据采集 → 假设 → 证据-假设关联 → 信号派生 → 规则排序
      → 报告生成 → 报告引用完整性闸门。

对照场景: Redis 连接池耗尽 (F-001), 故障开始 02:00。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import (
    EvidenceType,
    HypothesisEvidence,
    HypothesisStatus,
    RelationType,
    RootCauseHypothesis,
)
from traceops.evidence.normalizer import normalize
from traceops.evidence.store import EvidenceStore
from traceops.rca.ranking import derive_signals, rank_hypotheses
from traceops.rca.report import ReportIntegrityError, build_report

TS = datetime(2026, 10, 30, 2, 0, 0, tzinfo=UTC)
WINDOW = (TS - timedelta(minutes=15), TS + timedelta(minutes=15))


def ns(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1e9))


def build_redis_incident(store: EvidenceStore) -> dict:
    """F-001 现场: 4 条证据 + 2 个假设。"""
    for src in ("prometheus", "loki", "tempo", "eventbus"):
        store.register_source(src)

    metric = normalize(
        evidence_type=EvidenceType.METRIC, source_id="prometheus",
        incident_id="inc-001", run_id="inv-001", service_id="redis",
        query_template_id="redis_pool_active",
        query_params={"metric": "redis_pool_active"},
        raw_payload={"resultType": "vector", "result": [
            {"metric": {"service": "redis"}, "value": [int((TS - timedelta(minutes=2)).timestamp()), "50"]},
        ]},
        observed_at=TS - timedelta(minutes=2), window=WINDOW,
    )
    log = normalize(
        evidence_type=EvidenceType.LOG, source_id="loki",
        incident_id="inc-001", run_id="inv-001", service_id="order-service",
        query_template_id="order_redis_errors",
        query_params={"filter": "Redis connection timeout"},
        raw_payload={"streams": [{"stream": {"service": "order-service"},
                                  "values": [[ns(TS), "Redis connection timeout", {"trace_id": "t-1"}]]}]},
        observed_at=TS, window=WINDOW,
    )
    trace = normalize(
        evidence_type=EvidenceType.TRACE, source_id="tempo",
        incident_id="inc-001", run_id="inv-001", service_id="redis",
        query_template_id="slow_redis_spans",
        query_params={"min_duration_ms": 1000},
        raw_payload={"batches": [{"resource": {"service.name": "redis"},
                                  "spans": [{"traceID": "t-1", "spanID": "s-1", "operationName": "redis GET",
                                             "startTime": (TS - timedelta(minutes=1)).isoformat(),
                                             "duration": 3900,
                                             "tags": [{"key": "error", "value": True}]}]}]},
        observed_at=TS, window=WINDOW,
    )
    change = normalize(
        evidence_type=EvidenceType.EVENT, source_id="eventbus",
        incident_id="inc-001", run_id="inv-001", service_id="redis",
        query_template_id="config_changes",
        query_params={"type": "config_changed"},
        raw_payload={"event": "config_changed", "target": "redis-pool",
                     "key": "max_connections", "from": 200, "to": 20},
        observed_at=TS - timedelta(minutes=5), window=WINDOW,
    )

    h_redis = RootCauseHypothesis(
        id="h-redis", investigation_run_id="inv-001", candidate_service="redis",
        candidate_component="connection-pool", fault_type="pool_exhaustion",
        failure_mechanism="pool saturated after config change",
        predicted_observations=["pool_pending_increased", "redis_timeout_logs", "slow_redis_spans"],
    )
    h_mysql = RootCauseHypothesis(
        id="h-mysql", investigation_run_id="inv-001", candidate_service="mysql",
        candidate_component="innodb", fault_type="slow_query",
        failure_mechanism="unbounded query",
        predicted_observations=["slow_query_logs"],
    )

    links = [
        HypothesisEvidence(hypothesis_id="h-redis", evidence_id=store.add(metric).id,
                           relation_type=RelationType.SUPPORTS, created_by="rule",
                           reason="pool active at cap"),
        HypothesisEvidence(hypothesis_id="h-redis", evidence_id=store.add(log).id,
                           relation_type=RelationType.SUPPORTS, created_by="rule",
                           reason="redis timeouts in order service"),
        HypothesisEvidence(hypothesis_id="h-redis", evidence_id=store.add(trace).id,
                           relation_type=RelationType.SUPPORTS, created_by="rule",
                           reason="slow redis spans"),
        HypothesisEvidence(hypothesis_id="h-redis", evidence_id=store.add(change).id,
                           relation_type=RelationType.SUPPORTS, created_by="rule",
                           reason="pool shrunk 200 -> 20 five minutes before outage"),
    ]
    return dict(hypotheses=[h_redis, h_mysql], links=links,
                evidence=list(store.list(incident_id="inc-001")))


class TestEndToEnd:
    def test_redis_pool_exhaustion_diagnosed(self):
        store = EvidenceStore()
        ctx = build_redis_incident(store)

        signals = [derive_signals(h, ctx["links"], store, fault_start=TS)
                   for h in ctx["hypotheses"]]
        ranked = rank_hypotheses(ctx["hypotheses"], signals)

        assert ranked[0].hypothesis_id == "h-redis"
        assert ranked[0].status == HypothesisStatus.SUPPORTED
        assert "R-004" in ranked[0].applied_rules  # config change before fault

        report = build_report(
            investigation_run_id="inv-001",
            hypotheses=ctx["hypotheses"], links=ctx["links"],
            evidence=ctx["evidence"], ranked=ranked,
            fault_start=TS, store=store,
        )
        assert report.diagnosis_status == "SUPPORTED"
        assert not report.abstained
        assert "redis" in report.summary.lower()
        assert all(eid in store._evidence for eid in report.all_evidence_ids())
        assert store.validate_report_references(report).passed

    def test_no_evidence_abstains_instead_of_guessing(self):
        store = EvidenceStore()
        h = RootCauseHypothesis(
            id="h-x", investigation_run_id="inv-002", candidate_service="kafka",
            fault_type="consumer_lag", failure_mechanism="unknown",
            predicted_observations=["lag_increased"],
        )
        ranked = rank_hypotheses([h], [derive_signals(h, [], store, fault_start=TS)])
        assert ranked[0].status == HypothesisStatus.INCONCLUSIVE
        report = build_report(
            investigation_run_id="inv-002", hypotheses=[h], links=[],
            evidence=[], ranked=ranked, fault_start=TS, store=store,
            missing_data=["kafka lag metrics"],
        )
        assert report.diagnosis_status == "INCONCLUSIVE"
        assert report.abstained is True
        assert report.missing_data == ["kafka lag metrics"]


class TestReportGate:
    def test_report_with_fake_reference_rejected(self):
        store = EvidenceStore()
        ctx = build_redis_incident(store)
        signals = [derive_signals(h, ctx["links"], store, fault_start=TS) for h in ctx["hypotheses"]]
        ranked = rank_hypotheses(ctx["hypotheses"], signals)

        fake_link = HypothesisEvidence(
            hypothesis_id="h-redis", evidence_id="e-fabricated",
            relation_type=RelationType.SUPPORTS, created_by="model",
        )
        with pytest.raises(ReportIntegrityError, match="e-fabricated"):
            build_report(
                investigation_run_id="inv-001",
                hypotheses=ctx["hypotheses"], links=ctx["links"] + [fake_link],
                evidence=ctx["evidence"], ranked=ranked,
                fault_start=TS, store=store,
            )
