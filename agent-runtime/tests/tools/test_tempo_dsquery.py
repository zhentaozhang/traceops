"""ds/query 路径：不依赖 Tempo 版本也能取回完整 span 树。

实测依据（当前栈 Grafana 11.1.0 + Tempo 2.5.0）：
  mcp-grafana get_tempo_trace -> /api/v2/traces/{id} -> 404
  Grafana /api/ds/query + queryType=traceId -> 200，返回
  traceID/spanID/parentSpanID/operationName/serviceName/kind/statusCode/statusMessage
"""
from __future__ import annotations

from traceops.domain.models import EvidenceType
from traceops.evidence.normalizer import normalize
from traceops.orchestration.graph import _unwrap_raw
from traceops.tools.tempo_dsquery import _frame_to_spans, dsquery_frames_to_batches

# 实测抄回的真实帧结构（列式）
REAL_DSQ = {
    "results": {"A": {"status": 200, "frames": [{
        "schema": {"fields": [{"name": n} for n in (
            "traceID", "spanID", "parentSpanID", "operationName",
            "serviceName", "kind", "statusCode", "statusMessage")]},
        "data": {"values": [
            ["aa11bb22cc33dd44ee55ff6677889900"],
            ["00f067aa0ba902b7"],
            [""],
            ["redis.command"],
            ["redis"],
            ["SPAN_KIND_CLIENT"],
            [2],
            ["pool exhausted"],
        ]},
    }]}},
}


class TestFrameConversion:
    def test_columns_become_rows(self):
        spans = _frame_to_spans(REAL_DSQ["results"]["A"]["frames"][0])
        assert len(spans) == 1
        assert spans[0]["operationName"] == "redis.command"

    def test_empty_frame_is_empty(self):
        assert _frame_to_spans({"schema": {"fields": []}, "data": {"values": []}}) == []

    def test_batches_grouped_by_service(self):
        batches = dsquery_frames_to_batches(REAL_DSQ)
        assert len(batches) == 1
        attrs = batches[0]["resource"]["attributes"]
        assert attrs[0]["value"]["stringValue"] == "redis"


class TestNormalizesIntoEvidence:
    def test_dsquery_result_becomes_trace_evidence(self):
        """ds/query 结果必须能一路归一化成 Evidence（含 error 状态与父 span）。"""
        from datetime import UTC, datetime
        batches = dsquery_frames_to_batches(REAL_DSQ)
        raw = {"traces_considered": 1, "batches": batches, "degraded": False}
        now = datetime.now(UTC)
        evidence = normalize(
            evidence_type=EvidenceType.TRACE, source_id="grafana-dsquery",
            incident_id="i", run_id="r", service_id="redis",
            query_template_id="qt", query_params={},
            raw_payload=_unwrap_raw(raw, EvidenceType.TRACE),
            observed_at=now, window=(now, now),
        )
        assert evidence.normalized_data["total_records"] == 1
        span = evidence.normalized_data["spans"][0]
        assert span["error"] is True, "statusCode=2 must map to an error span"
        assert span["operation_name"] == "redis.command"

    def test_error_status_mapping(self):
        assert dsquery_frames_to_batches(REAL_DSQ)[0]["scopeSpans"][0]["spans"][0]["status"]["code"] == "STATUS_CODE_ERROR"
