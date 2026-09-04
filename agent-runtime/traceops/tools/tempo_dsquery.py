"""Grafana `/api/ds/query` 是一条**不依赖 Tempo 版本**的 trace-by-id 路径。

## 为什么需要它（实测）

| 路径 | 当前栈（Grafana 11.1 + Tempo 2.5） |
|---|---|
| mcp-grafana `get_tempo_trace` → `/api/v2/traces/{id}` | ❌ 404（Tempo 2.5 无该端点） |
| **Grafana `/api/ds/query` + `queryType=traceId`** | ✅ **200，返回完整 span 帧** |

实测返回的帧字段：`traceID / spanID / parentSpanID / operationName /
serviceName / kind / statusCode / statusMessage` —— 比 search 的 spanSet
**更多**（有 parentSpanID，可以重建 span 树）。

所以有两个独立选择：
1. 升 Tempo ≥ 2.6，让 mcp-grafana 的原生路径可用；
2. **保持 Tempo 2.5，改走 ds/query**。

这条路径的意义：不必为了拿 trace 而升级整个存储层。

## 形状

ds/query 返回 Grafana DataFrame（列式），必须转成 OTLP `batches`
才能进 `TraceNormalizer`：

```
{"results": {"A": {"status": 200, "frames": [
    {"schema": {"fields": [{"name": "traceID"}, ...]},
     "data": {"values": [[...], [...]]}}]}}}
```
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any


def _frame_to_spans(frame: dict[str, Any]) -> list[dict[str, Any]]:
    """Grafana 列式帧 → 行式 span 列表。"""
    fields = [f.get("name") for f in frame.get("schema", {}).get("fields", [])]
    values = frame.get("data", {}).get("values") or []
    if not fields or not values:
        return []
    row_count = len(values[0]) if values else 0
    spans: list[dict[str, Any]] = []
    for i in range(row_count):
        row = {name: (values[idx][i] if idx < len(values) and i < len(values[idx]) else None)
               for idx, name in enumerate(fields)}
        spans.append(row)
    return spans


def dsquery_frames_to_batches(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """`/api/ds/query` 的 traceId 响应 → OTLP `batches`。

    一个 resourceSpan 承载全部 span（service 名取自 span 的 serviceName，
    因此按 service 分组，避免把多服务 trace 混成一个 batch）。
    """
    by_service: dict[str, list[dict[str, Any]]] = {}
    for result in (payload.get("results") or {}).values():
        for frame in result.get("frames") or []:
            for span in _frame_to_spans(frame):
                service = span.get("serviceName") or ""
                by_service.setdefault(service, []).append(span)

    batches: list[dict[str, Any]] = []
    for service, spans in by_service.items():
        otlp_spans = []
        for s in spans:
            # statusCode: Grafana 用 int（0=UNSET,1=OK,2=ERROR）或字符串
            raw_status = s.get("statusCode")
            code = "STATUS_CODE_ERROR" if raw_status in (2, "2", "STATUS_CODE_ERROR") else "STATUS_CODE_UNSET"
            otlp_spans.append({
                "traceId": s.get("traceID"),
                "spanId": s.get("spanID"),
                "parentSpanId": s.get("parentSpanID"),
                "name": s.get("operationName"),
                "startTimeUnixNano": s.get("startTimeUnixNano") or s.get("startTime"),
                "endTimeUnixNano": s.get("endTimeUnixNano") or s.get("endTime"),
                "status": {"code": code, "message": s.get("statusMessage")},
            })
        batches.append({
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": service}}]},
            "scopeSpans": [{"spans": otlp_spans}],
        })
    return batches


class GrafanaDsQueryClient:
    """调用 Grafana `/api/ds/query` 取 trace-by-id。

    只做一件事：把 (grafana_url, datasource_uid, trace_id) 变成 OTLP batches。
    鉴权走传入的 headers（Service Account token 或 Basic）。
    """

    def __init__(self, base_url: str, *, datasource_uid: str = "tempo",
                 headers: dict[str, str] | None = None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.datasource_uid = datasource_uid
        self.headers = dict(headers or {})
        self.timeout = timeout

    def get_trace_batches(self, trace_id: str) -> list[dict[str, Any]]:
        payload = {
            "queries": [{
                "refId": "A",
                "datasource": {"type": "tempo", "uid": self.datasource_uid},
                "queryType": "traceId",
                "query": trace_id,
            }],
            # from/to = "0" 让 Tempo 跨全部时间查，不因时间窗错过 trace
            "from": "0",
            "to": "0",
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/ds/query",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **self.headers},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode())
        return dsquery_frames_to_batches(body)
