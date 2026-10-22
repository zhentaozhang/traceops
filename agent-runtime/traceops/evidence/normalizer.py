"""Evidence Normalizer (blueprint §6.2): 数据源格式 → 统一 Evidence。

确定性转换，无 LLM 参与。每个适配器:
1. 校验 payload 形状（不是本类型的数据直接拒绝）
2. 类型化字段名归一（operationName → operation_name 等）
3. 提升跨类型关联字段（trace_id / span_id）到 Evidence 顶层
4. 保留原始计数与截断标记，防上下文膨胀
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

from traceops.domain.models import Evidence, EvidenceType

DEFAULT_MAX_RECORDS = 500


def _norm_tags(tags: list[dict[str, Any]] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for t in tags or []:
        key = t.get("key")
        if key:
            out[key] = t.get("value")
    return out


#: 秒级现代时间戳约 1.7e9，毫秒级约 1.7e12。1e11 正好把两者分开。
_SECONDS_THRESHOLD = 1e11


def coerce_epoch(value: Any) -> datetime:
    """把可能是秒/毫秒/微秒/纳秒的 epoch 值统一成 aware datetime。

    为什么要自适应而不是写死 `/1e9`：Loki push **规范**要求纳秒字符串，
    但生态里毫秒/秒级很常见（某些 Alloy/Promtail 配置、自建 shipper）。
    写死纳秒时，一个秒级时间戳 `1793354520` 会被解析成 **1970-01-01**（差 56 年）。

    这种错误比"解析失败"更难排查：如果不触发窗口校验，它会**静默通过**
    并污染整条时间线，让 `observed_after_predecessor` 全部失真。

    借鉴 TriageRCA `_normalize_logs` 的做法（按数量级归一）：

        while ts.max() > 1e11: ts /= 1000

    这里对单值做等价判断：
        < 1e11  → 秒
        < 1e14  → 毫秒
        < 1e17  → 微秒
        否则    → 纳秒
    """
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"cannot coerce epoch value: {value!r}") from exc
    if number < 0:
        raise ValueError(f"epoch must be non-negative, got {number}")

    if number < _SECONDS_THRESHOLD:
        seconds = number
    elif number < _SECONDS_THRESHOLD * 1e3:
        seconds = number / 1e3
    elif number < _SECONDS_THRESHOLD * 1e6:
        seconds = number / 1e6
    else:
        seconds = number / 1e9
    return datetime.fromtimestamp(seconds, tz=UTC)


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
        # **空结果不是证据**。与 TraceNormalizer / LogNormalizer 保持一致：
        # "查了，什么都没有"不能变成一条零系列的证据 ——
        # 那会让报告引用一批空东西，并把"没看见"记成"看见了"。
        # 真的查不了（数据源挂了）由适配器抛异常，走 ERROR 路径。
        if not series:
            raise ValueError("metric payload: contains no series (an empty result is not evidence)")
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
                raw_ts, line = entry[0], entry[1]
                meta = entry[2] if len(entry) > 2 else {}
                # 单位自适应：纳秒是规范，但生态里有毫秒/秒级
                ts = coerce_epoch(raw_ts).isoformat()
                rec = {
                    "timestamp": ts,
                    "message": line,
                    "labels": labels,
                    "trace_id": meta.get("trace_id"),
                    "span_id": meta.get("span_id"),
                }
                records.append(rec)
        # **空结果不是证据**（与 metric / trace 同策略）：
        # 一条零行的日志证据会让"查了，没有"冒充"查到了"。
        if not records:
            raise ValueError("log payload: contains no records (an empty result is not evidence)")
        return {"kind": self.kind, "records": records,
                "total_records": total, "truncated": truncated}


def _decode_otlp_id(value: str | None) -> str | None:
    """把 OTLP 的 traceId/spanId 归一为十六进制。

    **两种编码都要处理**，这是实测出来的差异：
    - OTLP 规范：base64（如 `S/kvNXezTaajzpKdDg5HNg==`）
    - Tempo 2.5 的 /api/traces 实际返回：也可能是十六进制（如 `4bf92f35...`）

    区分方式：十六进制是纯 [0-9a-f] 且长度 32/16；base64 含 `=` 或非 hex 字符。
    不要用"先试 base64"的方式判断 —— 纯 hex 字符串也能被 base64 解码成垃圾。
    """
    if not value:
        return None
    lowered = value.strip().lower()
    hex_chars = set("0123456789abcdef")
    if lowered and all(c in hex_chars for c in lowered) and len(lowered) in (16, 32):
        return lowered  # 已经是十六进制
    try:
        return base64.b64decode(value).hex()
    except Exception:
        return value  # 无法解码：原样返回，不丢数据


def _otlp_resource_service(resource: dict[str, Any] | None) -> str | None:
    """OTLP 把服务名放在 resource.attributes 数组里，而不是 resource["service.name"]。"""
    for attribute in (resource or {}).get("attributes", []) or []:
        if attribute.get("key") == "service.name":
            value = attribute.get("value") or {}
            return value.get("stringValue") or value.get("value")
    return (resource or {}).get("service.name")


def _otlp_resource_tags(resource: dict[str, Any] | None) -> dict[str, Any]:
    tags: dict[str, Any] = {}
    for attribute in (resource or {}).get("attributes", []) or []:
        key = attribute.get("key")
        if not key:
            continue
        value = attribute.get("value") or {}
        tags[key] = value.get("stringValue", value.get("intValue", value.get("boolValue")))
    return tags


def _is_otlp_span(span: dict[str, Any]) -> bool:
    return "traceId" in span or "startTimeUnixNano" in span


def _trace_batches(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """把三种 trace 载荷形状统一成 `batches` 列表。

    为什么必须支持多种外壳：**`batches` 是项目内部约定**（Tempo 查询 API
    `/api/traces/{id}` 的返回形状），而 OTLP 规范与 push 端点用的外层键是
    `resourceSpans`。只认 `batches` 会让任何标准 OTLP 载荷被当成"形状错误"丢弃。

    这不是理论问题：TraceOps 的自观测第一次跑就撞上了 ——
    它自己按 OTLP 规范产出的 trace 过不了自己的 Normalizer。
    同样的缺口会让它丢弃外部系统送来的标准 OTLP 数据。

    接受的三种形状：
      1. `{"batches": [resourceSpan, ...]}`        —— 内部约定
      2. `{"resourceSpans": [resourceSpan, ...]}`  —— OTLP 规范
      3. 单个 `resourceSpan`（带 scopeSpans）      —— 部分 SDK 直接给这层
    """
    if not isinstance(raw, dict):
        raise ValueError(f"trace payload must be an object, got {type(raw).__name__}")
    if "batches" in raw:
        return list(raw["batches"] or [])
    if "resourceSpans" in raw:
        return list(raw["resourceSpans"] or [])
    if "scopeSpans" in raw or "instrumentationLibrarySpans" in raw or "spans" in raw:
        return [raw]
    raise ValueError(
        "trace payload: expected one of {batches: [...]} (Tempo query API) "
        "or {resourceSpans: [...]} (OTLP spec)")


class TraceNormalizer:
    """Trace 归一化。

    **同时支持两种格式**，这是被真实环境抓出来的必要性：
    - Jaeger 风格：`batches[].spans[]`，`traceID` 十六进制，直接给 `duration`
    - OTLP 标准：`batches[].scopeSpans[].spans[]`，`traceId` base64，
      只有 `startTimeUnixNano` / `endTimeUnixNano`，duration 要算差值

    真实 Tempo 的 `/api/traces/{id}` 返回的是 OTLP。只支持 Jaeger 会让所有
    真实 trace 证据被判为"空 trace"而丢弃 —— 假传输层永远发现不了这个问题，
    因为假数据是按 Normalizer 的期望造的。
    """

    kind = "trace"

    def normalize(self, raw: dict[str, Any], max_records: int) -> dict[str, Any]:
        batches = _trace_batches(raw)
        spans: list[dict[str, Any]] = []
        for batch in batches:
            resource = batch.get("resource") or {}
            # OTLP：scopeSpans 是数组；Jaeger：spans 直接在 batch 下
            otlp_scopes = batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []
            if otlp_scopes:
                service = _otlp_resource_service(resource)
                resource_tags = _otlp_resource_tags(resource)
                for scope in otlp_scopes:
                    for sp in scope.get("spans", []) or []:
                        start = sp.get("startTimeUnixNano")
                        end = sp.get("endTimeUnixNano")
                        duration_us = None
                        if start is not None and end is not None:
                            try:
                                duration_us = (int(end) - int(start)) // 1000
                            except (TypeError, ValueError):
                                duration_us = None
                        status = sp.get("status") or {}
                        spans.append({
                            "trace_id": _decode_otlp_id(sp.get("traceId")),
                            "span_id": _decode_otlp_id(sp.get("spanId")),
                            "service": service,
                            "operation_name": sp.get("name"),
                            "start_time": start,
                            "duration_us": duration_us,
                            "error": status.get("code") == "STATUS_CODE_ERROR",
                            "tags": {**resource_tags, "status.message": status.get("message")},
                        })
                continue

            service = resource.get("service.name")
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


class ProfileNormalizer:
    """Pyroscope 持续剖析 → 热点帧列表。

    为什么需要这一维：对「为什么慢」类根因，profile 比 trace 更直接 ——
    trace 说"哪个调用慢"，profile 说"慢在哪个函数"。

    接受的形状（Pyroscope `/api/v1/flamegraph`）：
    ```
    {"flamegraph": {"names": [...], "values": [...self samples...]},
     "metadata": {"sampleRate": ..., "spyName": ..., "units": ...}}
    ```

    只提取 **self value 最高的 N 帧**，不做调用树重建：
    证据需要的是"热点在哪"，不是完整火焰图（后者对模型是纯噪音）。
    """

    kind = "profile"
    #: 最多保留的热点帧数。超过这个数量对诊断没有增益，只会膨胀证据。
    MAX_HOT_FRAMES = 20
    #: 聚合根帧名。它们的 value 是整棵树的合计，因此永远排最前，
    #: 但它们不是任何具体函数 —— 报"热点是 total"没有任何诊断价值。
    _ROOT_FRAMES = ("total", "(root)", "root", "all")

    def normalize(self, raw: dict[str, Any], max_records: int) -> dict[str, Any]:
        if not isinstance(raw, dict) or "flamegraph" not in raw:
            raise ValueError(
                "profile payload: expected Pyroscope shape {flamegraph: {names, values}}")
        flame = raw["flamegraph"] or {}
        names = flame.get("names") or []
        values = flame.get("values") or []
        if not names or not values:
            raise ValueError("profile payload: flamegraph has no names/values")

        frames: list[dict[str, Any]] = []
        for name, value in zip(names, values):
            label = str(name).strip()
            if label.lower() in self._ROOT_FRAMES:
                # 聚合根节点：不是具体函数，排除
                continue
            try:
                self_value = int(value)
            except (TypeError, ValueError):
                continue
            frames.append({"frame": label, "self_value": self_value})

        # 最热在前 —— 调用方不该还要自己排序
        frames.sort(key=lambda f: f["self_value"], reverse=True)
        limit = min(self.MAX_HOT_FRAMES, max_records) if max_records else self.MAX_HOT_FRAMES
        hot = frames[:limit]

        metadata = raw.get("metadata") or {}
        return {
            "kind": self.kind,
            "total_records": len(frames),
            "hot_frames": hot,
            "truncated": len(hot) < len(frames),
            "sample_rate": metadata.get("sampleRate"),
            "spy_name": metadata.get("spyName"),
            "units": metadata.get("units"),
        }


_NORMALIZERS: dict[EvidenceType, Any] = {
    EvidenceType.METRIC: MetricNormalizer(),
    EvidenceType.LOG: LogNormalizer(),
    EvidenceType.TRACE: TraceNormalizer(),
    EvidenceType.EVENT: EventNormalizer(),
    EvidenceType.PROFILE: ProfileNormalizer(),
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
    collected_at: datetime | None = None,
) -> Evidence:
    """统一入口: 类型分派适配器 → 归一化 → 组装 Evidence。"""
    if evidence_type not in _NORMALIZERS:
        raise ValueError(f"no normalizer for evidence type {evidence_type!r}")
    norm = _NORMALIZERS[evidence_type].normalize(raw_payload, max_records)
    # 采集层可能标记"这是一份降级数据"（如 Tempo 只能用 search 的 spanSet 兜底）。
    # 该标记必须跟着证据走：引用完整性校验能过，但语义上它不是完整 span 树，
    # 丢掉标记等于默许下游把摘要当全量引用。
    if isinstance(raw_payload, dict) and raw_payload.get("degraded"):
        norm["degraded"] = True
        norm["degraded_reason"] = raw_payload.get("degraded_reason", "")

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
        # 从缓存里拿到的数据必须带着**当初**取回它的时刻：
        # `observed_at` 说数据描述的是哪一刻，`collected_at` 说我们什么时候拿到它。
        # 用 now() 会让报告声称"这份日志是 10:05 抓的"，而实际是 09:50 抓的。
        **({"collected_at": max(collected_at, observed_at)}
           if collected_at is not None else {}),
    )
    evidence._raw_payload = raw_payload  # noqa: SLF001 — 供 Store 快照，不序列化
    return evidence
