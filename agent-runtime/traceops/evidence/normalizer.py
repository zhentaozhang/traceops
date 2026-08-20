"""Evidence Normalizer (blueprint §6.2): 数据源格式 → 统一 Evidence。

确定性转换，无 LLM 参与。每个适配器:
1. 校验 payload 形状（不是本类型的数据直接拒绝）
2. 类型化字段名归一（operationName → operation_name 等）
3. 提升跨类型关联字段（trace_id / span_id）到 Evidence 顶层
4. 保留原始计数与截断标记，防上下文膨胀
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from traceops.domain.models import Evidence, EvidenceType

DEFAULT_MAX_RECORDS = 500


class TimeRange(BaseModel):
    start: datetime
    end: datetime


def _norm_tags(tags: list[dict[str, Any]] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for t in tags or []:
        key = t.get("key")
        if key:
            out[key] = t.get("value")
    return out


class MetricNormalizer:
    kind = "metric"

    def normalize(self, raw: dict[str, Any], max_records: int) -> dict[str, Any]:
        # Prometheus query result: {resultType: matrix|vector, result: [...]}
        if "resultType" not in raw or "result" not in raw:
            raise ValueError("metric payload: expected Prometheus result shape {resultType, result}")
        series: list[dict[str, Any]] = []
        for item in raw["result"]:
            if "values" in item:  # matrix
                values = [[ts, float(v)] for ts, v in item["values"]]
                series.append({"metric": item.get("metric", {}), "values": values})
            elif "value" in item:  # vector
                ts, v = item["value"]
                series.append({"metric": item.get("metric", {}), "value": [ts, float(v)]})
        return {"kind": self.kind, "result_type": raw["resultType"],
                "series": series, "result_count": len(series),
                "total_records": len(series), "truncated": False}


class LogNormalizer:
    kind = "log"

    def normalize(self, raw: dict[str, Any], max_records: int) -> dict[str, Any]:
        # Loki query result: {streams: [{stream: {...}, values: [[ns, line, meta?], ...]}]}
        if "streams" not in raw:
            raise ValueError("log payload: expected Loki result shape {streams: [...]}")
        records: list[dict[str, Any]] = []
        total = 0
        truncated = False
        for stream in raw["streams"]:
            labels = stream.get("stream", {})
            for entry in stream.get("values", []):
                total += 1
                if len(records) >= max_records:
                    truncated = True
                    continue
                ns, line = entry[0], entry[1]
                meta = entry[2] if len(entry) > 2 else {}
                ts = datetime.fromtimestamp(int(ns) / 1e9, tz=UTC).isoformat()
                rec = {
                    "timestamp": ts,
                    "message": line,
                    "labels": labels,
                    "trace_id": meta.get("trace_id"),
                    "span_id": meta.get("span_id"),
                }
                records.append(rec)
        return {"kind": self.kind, "records": records,
                "total_records": total, "truncated": truncated}


class TraceNormalizer:
    kind = "trace"

    def normalize(self, raw: dict[str, Any], max_records: int) -> dict[str, Any]:
        # Tempo/Jaeger trace: {batches: [{resource: {...}, spans: [...]}]}
        if "batches" not in raw:
            raise ValueError("trace payload: expected Tempo shape {batches: [...]}")
        spans: list[dict[str, Any]] = []
        for batch in raw["batches"]:
            service = (batch.get("resource") or {}).get("service.name")
            for sp in batch.get("spans", []):
                tags = _norm_tags(sp.get("tags"))
                spans.append({
                    "trace_id": sp.get("traceID"),
                    "span_id": sp.get("spanID"),
                    "service": service,
                    "operation_name": sp.get("operationName"),
                    "start_time": sp.get("startTime"),
                    "duration_us": sp.get("duration"),
                    "error": bool(tags.get("error", False)),
                    "tags": tags,
                })
        spans = spans[:max_records]
        if not spans:
            raise ValueError("trace payload: contains no spans (empty traces are not valid evidence)")
        return {"kind": self.kind, "spans": spans,
                "total_records": len(spans), "truncated": len(spans) < len(spans) or False}


class EventNormalizer:
    """变更事件（配置变更/部署/重启）→ Evidence。F-005 级联故障的时间线基础。"""

    kind = "event"

    def normalize(self, raw: dict[str, Any], max_records: int) -> dict[str, Any]:
        if "events" in raw:
            events = list(raw["events"])
        elif "event" in raw:
            events = [raw]
        else:
            raise ValueError("event payload: expected {event: ...} or {events: [...]}")
        if not events:
            raise ValueError("event payload: contains no events")
        trimmed = events[:max_records]
        for ev in trimmed:
            ev.setdefault("timestamp", None)
        return {"kind": self.kind, "events": trimmed,
                "total_records": len(events), "truncated": len(trimmed) < len(events)}


_NORMALIZERS: dict[EvidenceType, Any] = {
    EvidenceType.METRIC: MetricNormalizer(),
    EvidenceType.LOG: LogNormalizer(),
    EvidenceType.TRACE: TraceNormalizer(),
    EvidenceType.EVENT: EventNormalizer(),
}


def normalize(
    *,
    evidence_type: EvidenceType,
    source_id: str,
    incident_id: str,
    run_id: str,
    service_id: str | None,
    query_template_id: str,
    query_params: dict[str, Any],
    raw_payload: dict[str, Any],
    observed_at: datetime,
    window: tuple[datetime, datetime],
    trace_id: str | None = None,
    span_id: str | None = None,
    source_link: str | None = None,
    rendered_query: str | None = None,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> Evidence:
    """统一入口: 类型分派适配器 → 归一化 → 组装 Evidence。"""
    if evidence_type not in _NORMALIZERS:
        raise ValueError(f"no normalizer for evidence type {evidence_type!r}")
    norm = _NORMALIZERS[evidence_type].normalize(raw_payload, max_records)

    # promote correlation fields from first record/span if not given explicitly
    if trace_id is None:
        if norm["kind"] == "log" and norm["records"]:
            trace_id = norm["records"][0].get("trace_id")
        elif norm["kind"] == "trace" and norm["spans"]:
            trace_id = norm["spans"][0].get("trace_id")
    if span_id is None and norm["kind"] == "trace" and norm["spans"]:
        span_id = norm["spans"][0].get("span_id")

    evidence = Evidence(
        incident_id=incident_id,
        investigation_run_id=run_id,
        evidence_type=evidence_type,
        source_id=source_id,
        service_id=service_id,
        observed_at=observed_at,
        window_start=window[0],
        window_end=window[1],
        query_template_id=query_template_id,
        query_params=query_params,
        normalized_data=norm,
        trace_id=trace_id,
        span_id=span_id,
        source_link=source_link,
        rendered_query=rendered_query,
    )
    evidence._raw_payload = raw_payload  # noqa: SLF001 — 供 Store 快照，不序列化
    return evidence
