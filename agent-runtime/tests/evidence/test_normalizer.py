"""Cycle 2 RED: Evidence Normalizer tests.

三种数据源适配器: Metric / Log / Trace → 统一 Evidence (blueprint §6.2)。
确定性: 相同输入 → 相同输出 (含顺序), 无 LLM 参与。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.evidence.normalizer import (
    LogNormalizer,
    MetricNormalizer,
    TraceNormalizer,
    normalize,
)
from traceops.domain.models import Evidence, EvidenceType

TS = datetime(2026, 10, 30, 2, 0, 0, tzinfo=UTC)
WINDOW = (TS - timedelta(minutes=5), TS + timedelta(minutes=5))


class TestMetricNormalizer:
    def test_prometheus_style_series(self):
        raw = {
            "resultType": "matrix",
            "result": [
                {"metric": {"__name__": "redis_pool_active", "service": "redis"},
                 "values": [[1769754000, "48"], [1769754060, "50"]]},
            ],
        }
        ev = normalize(
            evidence_type=EvidenceType.METRIC, source_id="prometheus",
            incident_id="inc-001", run_id="inv-001", service_id="redis",
            query_template_id="redis_pool_active",
            query_params={"query": "redis_pool_active{service=redis}"},
            raw_payload=raw, observed_at=TS, window=WINDOW,
        )
        assert ev.evidence_type == EvidenceType.METRIC
        assert ev.normalized_data["kind"] == "metric"
        assert ev.normalized_data["series"][0]["metric"]["__name__"] == "redis_pool_active"
        assert ev.normalized_data["series"][0]["values"][0] == [1769754000, 48.0]
        assert ev.normalized_data["result_count"] == 1

    def test_vector_result(self):
        raw = {"resultType": "vector", "result": [{"metric": {"service": "redis"}, "value": [1769754000, "50"]}]}
        ev = normalize(evidence_type=EvidenceType.METRIC, source_id="prometheus",
                       incident_id="inc-001", run_id="inv-001", service_id="redis",
                       query_template_id="q", query_params={}, raw_payload=raw,
                       observed_at=TS, window=WINDOW)
        assert ev.normalized_data["series"][0]["value"] == [1769754000, 50.0]

    def test_rejects_unexpected_metric_payload(self):
        with pytest.raises(ValueError, match="metric"):
            normalize(evidence_type=EvidenceType.METRIC, source_id="prometheus",
                      incident_id="inc-001", run_id="inv-001", service_id="redis",
                      query_template_id="q", query_params={},
                      raw_payload={"logs": ["not a metric"]},
                      observed_at=TS, window=WINDOW)


class TestLogNormalizer:
    def test_loki_style_streams(self):
        ns = int(TS.timestamp() * 1e9)
        raw = {
            "streams": [
                {"stream": {"service": "order-service", "level": "ERROR"},
                 "values": [
                     [str(ns), "Redis connection timeout", {"trace_id": "t-1", "span_id": "s-1"}],
                     [str(ns + 3_000_000_000), "Redis connection timeout", {"trace_id": "t-2", "span_id": "s-2"}],
                 ]},
            ],
        }
        ev = normalize(evidence_type=EvidenceType.LOG, source_id="loki",
                       incident_id="inc-001", run_id="inv-001", service_id="order-service",
                       query_template_id="order_redis_errors",
                       query_params={"filter": "Redis connection timeout"},
                       raw_payload=raw, observed_at=TS, window=WINDOW)
        assert ev.normalized_data["kind"] == "log"
        recs = ev.normalized_data["records"]
        assert len(recs) == 2
        assert recs[0]["timestamp"] == "2026-10-30T02:00:00+00:00"
        assert recs[0]["trace_id"] == "t-1"
        assert recs[0]["message"] == "Redis connection timeout"
        # trace_id promoted to top-level evidence field for correlation
        assert ev.trace_id == "t-1"

    def test_record_limit(self):
        ns = int(TS.timestamp() * 1e9)
        raw = {"streams": [{"stream": {"service": "x"}, "values": [[str(ns + i), "m", {}] for i in range(100)]}]}
        ev = normalize(evidence_type=EvidenceType.LOG, source_id="loki",
                       incident_id="inc-001", run_id="inv-001", service_id="x",
                       query_template_id="q", query_params={}, raw_payload=raw,
                       observed_at=TS, window=WINDOW, max_records=10)
        assert len(ev.normalized_data["records"]) == 10
        assert ev.normalized_data["total_records"] == 100
        assert ev.normalized_data["truncated"] is True

    def test_rejects_non_log_payload(self):
        with pytest.raises(ValueError, match="log"):
            normalize(evidence_type=EvidenceType.LOG, source_id="loki",
                      incident_id="inc-001", run_id="inv-001", service_id="x",
                      query_template_id="q", query_params={},
                      raw_payload={"result": []},
                      observed_at=TS, window=WINDOW)


class TestTraceNormalizer:
    def test_tempo_style_trace(self):
        raw = {
            "batches": [
                {"resource": {"service.name": "order-service"},
                 "spans": [
                     {"traceID": "t-9", "spanID": "sp-1", "operationName": "GET /orders",
                      "startTime": "2026-10-30T02:00:00Z", "duration": 4200,
                      "tags": [{"key": "error", "value": True}]},
                     {"traceID": "t-9", "spanID": "sp-2", "operationName": "redis GET",
                      "startTime": "2026-10-30T02:00:00.1Z", "duration": 3900,
                      "tags": [{"key": "error", "value": True}]},
                 ]},
            ],
        }
        ev = normalize(evidence_type=EvidenceType.TRACE, source_id="tempo",
                       incident_id="inc-001", run_id="inv-001", service_id="order-service",
                       query_template_id="slow_traces",
                       query_params={"min_duration": "1s"},
                       raw_payload=raw, observed_at=TS, window=WINDOW)
        assert ev.normalized_data["kind"] == "trace"
        assert ev.trace_id == "t-9"
        spans = ev.normalized_data["spans"]
        assert len(spans) == 2
        assert spans[1]["operation_name"] == "redis GET"
        assert spans[1]["error"] is True

    def test_rejects_empty_trace(self):
        with pytest.raises(ValueError, match="trace"):
            normalize(evidence_type=EvidenceType.TRACE, source_id="tempo",
                      incident_id="inc-001", run_id="inv-001", service_id="x",
                      query_template_id="q", query_params={},
                      raw_payload={"batches": []},
                      observed_at=TS, window=WINDOW)


class TestDeterminism:
    def test_same_input_same_output(self):
        raw = {"resultType": "vector", "result": [{"metric": {"service": "redis"}, "value": [1769754000, "50"]}]}
        kw = dict(evidence_type=EvidenceType.METRIC, source_id="prometheus",
                  incident_id="inc-001", run_id="inv-001", service_id="redis",
                  query_template_id="q", query_params={"x": 1}, raw_payload=raw,
                  observed_at=TS, window=WINDOW)
        e1, e2 = normalize(**kw), normalize(**kw)
        # ids differ (unique), but normalized content and collected-at-independent fields identical
        assert e1.normalized_data == e2.normalized_data
        assert e1.checksum == e2.checksum or (e1.checksum == "" and e2.checksum == "")
