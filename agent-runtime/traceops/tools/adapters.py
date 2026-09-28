"""数据源适配器（蓝图 §4.5 工具层、S2 Spike 的落地实现）。

设计要点：
- 传输层是可替换的：生产用 `UrllibTransport`，测试用 FakeTransport。
- 适配器只负责"请求构造 + 响应解析"，不做归一化（归一化在 evidence/normalizer.py）。
- 返回的 payload 与可观测系统原生格式一致，保证 Evidence 可追溯。
- `GrafanaMCPClient` 走 MCP JSON-RPC（streamable HTTP），响应可能是 JSON 或 SSE。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Protocol


class TransportError(RuntimeError):
    """网络层或数据源返回错误。"""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class MCPError(RuntimeError):
    """MCP Server 返回 JSON-RPC 错误或工具执行错误。"""


class HttpTransport(Protocol):
    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, Any]: ...

    def post(
        self,
        url: str,
        payload: Any,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, Any]: ...


class UrllibTransport:
    """标准库实现。生产路径，无第三方依赖。

    - 2xx 返回 (status, 解析后的 JSON；非 JSON 时返回文本)
    - 非 2xx 也返回 (status, body)，由调用方决定是否抛错（适配器统一抛 TransportError）
    """

    def _request(self, req: urllib.request.Request, timeout: float) -> tuple[int, Any]:
        """发请求，并**对瞬时故障重试**。

        为什么数据源也要重试：一次 TLS 抖动或数据源重启会让 `collect`
        把整个数据源记进 `missing_data` —— 那一类证据就全没了，
        覆盖率下降进而让语义闸门降级。而这类故障重试一次通常就恢复。

        为什么 4xx 不重试：查询模板写错、时间窗越界属于确定性错误，
        重试只会白等并浪费预算。

        注意重试次数保守（2 次）：一次调查要打多个数据源，
        每个都重试多次会让总耗时不可控，反而触发上层 deadline。
        """
        from traceops.runtime.retry_http import (
            RetryPolicy, is_retryable_status, with_retry,
        )

        def _attempt() -> tuple[int, Any]:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    return resp.status, _decode(raw, resp.headers.get("Content-Type", ""))
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if is_retryable_status(exc.code):
                    # 保留 status：上层要靠它区分"数据源暂时不可用"与"查询写错了"，
                    # 丢掉它会把可排查的错误变成不可排查的。
                    raise TransportError(
                        f"transient HTTP {exc.code} from {req.full_url}",
                        status=exc.code,
                        body=_decode(raw, exc.headers.get("Content-Type", "")
                                      if exc.headers else "")) from exc
                return exc.code, _decode(
                    raw, exc.headers.get("Content-Type", "") if exc.headers else "")
            except urllib.error.URLError as exc:
                raise TransportError(
                    f"request to {req.full_url} failed: {exc.reason}") from exc

        return with_retry(_attempt, policy=RetryPolicy(max_attempts=2, base_delay=0.5))

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, Any]:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        return self._request(req, timeout)

    def post(
        self,
        url: str,
        payload: Any,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, Any]:
        data = json.dumps(payload).encode()
        merged = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        merged.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=merged, method="POST")
        return self._request(req, timeout)


def _decode(raw: bytes, content_type: str) -> Any:
    text = raw.decode("utf-8", errors="replace")
    ctype = content_type.lower()
    if "text/event-stream" in ctype or text.lstrip().startswith("event:"):
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _to_epoch_seconds(value: str | datetime) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return _parse_iso(value).timestamp()


def _to_nanos(value: str | datetime) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1_000_000_000)
    return int(_parse_iso(value).timestamp() * 1_000_000_000)


def _parse_iso(value: str) -> datetime:
    # 容忍 Z 后缀
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _HttpAdapter:
    def __init__(self, base_url: str, *, transport: HttpTransport | None = None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibTransport()
        self.timeout = timeout

    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        status, body = self.transport.get(f"{self.base_url}{path}", params, timeout=self.timeout)
        if status != 200:
            raise TransportError(
                f"{self.base_url}{path} returned HTTP {status}", status=status, body=body
            )
        if isinstance(body, dict) and body.get("status") == "error":
            raise TransportError(f"{self.base_url}{path} returned error: {body.get('error')}", body=body)
        return body


class PrometheusAdapter(_HttpAdapter):
    def query_range(
        self,
        expression: str,
        start: str | datetime,
        end: str | datetime,
        step_seconds: int = 60,
    ) -> dict[str, Any]:
        return self._get_json(
            "/api/v1/query_range",
            {
                "query": expression,
                "start": _to_epoch_seconds(start),
                "end": _to_epoch_seconds(end),
                "step": step_seconds,
            },
        )

    def query_instant(self, expression: str, when: str | datetime | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"query": expression}
        if when is not None:
            params["time"] = _to_epoch_seconds(when)
        return self._get_json("/api/v1/query", params)

    def service_error_rate(
        self, service: str, start: str | datetime, end: str | datetime, step_seconds: int = 60
    ) -> dict[str, Any]:
        """5xx 占比。假设业务指标为 `http_requests_total{service,status}`。"""
        total = f'sum(rate(http_requests_total{{service="{service}"}}[1m]))'
        errors = f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[1m]))'
        expression = f"{errors} / clamp_min({total}, 0.000001)"
        return self.query_range(expression, start, end, step_seconds=step_seconds)


class LokiAdapter(_HttpAdapter):
    def query_range(
        self,
        query: str,
        start: str | datetime,
        end: str | datetime,
        *,
        limit: int = 200,
        direction: str = "backward",
    ) -> dict[str, Any]:
        return self._get_json(
            "/loki/api/v1/query_range",
            {
                "query": query,
                "start": str(_to_nanos(start)),
                "end": str(_to_nanos(end)),
                "limit": limit,
                "direction": direction,
            },
        )

    def service_logs(
        self,
        service: str,
        start: str | datetime,
        end: str | datetime,
        *,
        filter: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        logql = f'{{service="{service}"}}'
        if filter:
            logql += f' |= "{filter}"'
        return self.query_range(logql, start, end, limit=limit)


class TempoAdapter(_HttpAdapter):
    def search(
        self,
        service: str,
        start: str | datetime,
        end: str | datetime,
        *,
        limit: int = 20,
        min_duration_ms: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "tags": f"service.name={service}",
            "start": int(_to_epoch_seconds(start)),
            "end": int(_to_epoch_seconds(end)),
            "limit": limit,
        }
        if min_duration_ms is not None:
            params["minDuration"] = f"{min_duration_ms}ms"
        return self._get_json("/api/search", params)

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        return self._get_json(f"/api/traces/{trace_id}", {})

    def service_traces(
        self,
        service: str,
        start: str | datetime,
        end: str | datetime,
        *,
        limit: int = 20,
        min_duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """搜索 + 逐个拉取，合并成 TraceNormalizer 可直接消费的 OTLP 风格 payload。"""
        found = self.search(service, start, end, limit=limit, min_duration_ms=min_duration_ms)
        trace_ids = [t.get("traceID") for t in found.get("traces", []) if t.get("traceID")]
        batches: list[Any] = []
        for trace_id in trace_ids:
            payload = self.get_trace(trace_id)
            batches.extend(payload.get("batches", []))
        return {"traces_considered": len(trace_ids), "batches": batches}


def _mcp_time(value: str) -> str:
    """把 ISO8601 时间裁剪成 mcp-grafana 的时间解析器**能接受**的精度。

    实测 Prometheus 侧（gtime 解析器）：
        2026-10-31T03:36:00Z               OK
        2026-10-31T03:36:00.123Z           OK
        2026-10-31T03:36:00.123456+00:00   FAIL
          -> parsing end time: syntax error: unexpected tDIGIT ... starting with "0"

    而 `datetime.isoformat()` 默认输出 6 位微秒 —— 上游 `_plan_node` 传的正是它。
    所以参数名改对之后，**时间格式仍会让真实调用 100% 失败**，只是错误从
    "unknown arguments" 变成 "parse error"。

    只裁小数位，不做别的改写：
    - 相对时间（now / now-15m）原样返回，它们是服务端支持的格式
    - 已经是 Z 或 +00:00 且≤3 位小数的原样返回
    """
    if not isinstance(value, str) or "." not in value:
        return value
    head, _, tail = value.partition(".")
    # 拆出小数部分与后面的时区后缀（Z / +00:00 / -07:00）
    frac = ""
    rest = tail
    for i, ch in enumerate(tail):
        if not ch.isdigit():
            frac, rest = tail[:i], tail[i:]
            break
    else:
        frac, rest = tail, ""
    if len(frac) > 3:
        frac = frac[:3]
    return f"{head}.{frac}{rest}" if frac else f"{head}{rest}"


def _mcp_prometheus_to_native(raw: Any) -> Any:
    """Grafana MCP 的 query_prometheus 结果 → Prometheus 原生 query_range 形状。

    MCP 返回（实测）:
        {"data": [{"metric": {...}, "values": [[ts, "v"], ...]}], "exploreUrl": "..."}
    Prometheus 原生（MetricNormalizer 唯一认的形状）:
        {"status": "success", "data": {"resultType": "matrix", "result": [...]}}

    已经是原生形状时原样返回（幂等），避免重复转换破坏数据。
    """
    if not isinstance(raw, dict):
        return raw
    data = raw.get("data")
    # 原生形状：data 是 dict 且带 result
    if isinstance(data, dict) and "result" in data:
        return raw
    if not isinstance(data, list):
        return raw
    # 判断 matrix 还是 vector：有 values 列表 = matrix，有 value = vector
    result_type = "matrix"
    if data and all("value" in item and "values" not in item for item in data if isinstance(item, dict)):
        result_type = "vector"
    return {"status": "success", "data": {"resultType": result_type, "result": data}}


def _mcp_loki_to_native(raw: Any) -> Any:
    """Grafana MCP 的 query_loki_logs 结果 → Loki 原生 query_range 形状。

    MCP 返回（实测）**逐条摊平**，而且 timestamp 是纳秒字符串：
        {"data": [{"timestamp": "1793417622798029056", "line": "...",
                   "labels": {...}, "structuredMetadata": {...}}], "metadata": {...}}
    Loki 原生（LogNormalizer 唯一认的形状）**按 stream 聚合**:
        {"status": "success", "data": {"resultType": "streams",
         "result": [{"stream": {...labels...}, "values": [[ns, line, meta?]]}]}}

    转换要点：按 labels 分组，否则 N 条日志会变成 N 个 stream（下游逐条建证据，
    证据数量爆炸且丢失"同一批查询"的语义）。
    已经是原生形状时原样返回。
    """
    if not isinstance(raw, dict):
        return raw
    data = raw.get("data")
    if isinstance(data, dict) and "result" in data:
        return raw
    if not isinstance(data, list):
        return raw

    by_stream: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        labels = entry.get("labels") or {}
        # 用排序后的 JSON 做分组键：labels 是 dict，不可哈希
        key = json.dumps(labels, sort_keys=True, ensure_ascii=False)
        stream = by_stream.get(key)
        if stream is None:
            stream = {"stream": dict(labels), "values": []}
            by_stream[key] = stream
            order.append(key)
        ts = str(entry.get("timestamp", ""))
        line = entry.get("line", "")
        meta = entry.get("structuredMetadata") or None
        # Loki values 元素是 [ns, line] 或 [ns, line, metadata]
        if meta:
            stream["values"].append([ts, line, meta])
        else:
            stream["values"].append([ts, line])
    return {
        "status": "success",
        "data": {"resultType": "streams", "result": [by_stream[k] for k in order]},
    }


def _mcp_tempo_trace_to_batches(payload: Any) -> list[Any]:
    """`get_tempo_trace` 的响应 → OTLP `batches` 列表。

    两个真实形状都要认，否则证据会静默丢失：

    - **Tempo 2.x / Grafana Tempo datasource**：`{"batches": [resourceSpan...]}`
    - **Tempo v3.0.0 /api/v2/traces**：`{"trace": {"resourceSpans": [...]}, "metrics": {}}`

    旧实现写的是 `payload.get("batches", [payload])`：对 v3 来说 `batches` 不存在，
    于是把**外层 dict** 当成一个 batch 塞进去。外层没有 spans/scopeSpans，
    `TraceNormalizer` 判 "contains no spans"，整条 trace 证据丢失 ——
    而且这条丢失路径是静默的（进 missing_data，报告照出）。
    """
    if not isinstance(payload, dict):
        return []
    if "batches" in payload:
        return list(payload["batches"] or [])
    trace = payload.get("trace")
    if isinstance(trace, dict):
        # v3：trace 内部本身就是 resourceSpans 的容器
        spans = trace.get("resourceSpans")
        if isinstance(spans, list):
            return spans
        if "batches" in trace:
            return list(trace["batches"] or [])
    # 未包装的单个 resourceSpan（少数实现直接返回这层）
    if "resourceSpans" in payload and isinstance(payload["resourceSpans"], list):
        return payload["resourceSpans"]
    return []


def _mcp_tempo_search_to_batches(trace: Any) -> list[Any]:
    """search_tempo_traces 的单条 trace 摘要 → OTLP `batches` 形状。

    为什么需要：某些版本组合下 `get_tempo_trace` 恒 404（实测 Grafana 11.1.0
    + Tempo 2.5.0 —— mcp-grafana 走 `/api/v2/traces`，Tempo 2.5 没有该端点）。
    但 search 的响应**已经带回 spanSet**：
        {"traceID", "rootServiceName", "rootTraceName",
         "startTimeUnixNano", "durationMs", "spanSet": {"spans": [{spanID, ...}]}}

    用它构造的是一份**降级**证据：只有匹配到的 span，没有完整 span 树，
    也没有父子关系。丢掉它的代价是"整条 trace 证据消失"，比降级更糟；
    所以保留，但调用方必须靠 `degraded=True` 知道这不是完整数据。

    产出形状对齐 TraceNormalizer 的 OTLP 分支（scopeSpans/spans + UnixNano）。
    """
    if not isinstance(trace, dict):
        return []
    span_set = trace.get("spanSet") or {}
    raw_spans = span_set.get("spans") or []
    if not raw_spans:
        return []

    service = trace.get("rootServiceName") or ""
    root_name = trace.get("rootTraceName") or ""
    spans: list[dict[str, Any]] = []
    for sp in raw_spans:
        if not isinstance(sp, dict):
            continue
        start_ns = sp.get("startTimeUnixNano") or trace.get("startTimeUnixNano")
        duration_ns = sp.get("durationNanos")
        end_ns = None
        if start_ns is not None and duration_ns is not None:
            try:
                end_ns = str(int(start_ns) + int(duration_ns))
            except (TypeError, ValueError):
                end_ns = None
        spans.append({
            "traceId": trace.get("traceID"),
            "spanId": sp.get("spanID"),
            "name": sp.get("name") or root_name,
            "startTimeUnixNano": start_ns,
            "endTimeUnixNano": end_ns,
        })
    return [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"spans": spans}],
    }]


class GrafanaMCPClient:
    """Grafana MCP Server 的 JSON-RPC 客户端（streamable HTTP，支持 SSE 响应）。"""

    def __init__(
        self,
        base_url: str,
        *,
        transport: HttpTransport | None = None,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url
        self.transport = transport or UrllibTransport()
        self.timeout = timeout
        self.headers = headers or {}
        self._next_id = 0

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        status, body = self.transport.post(self.base_url, payload, headers=self.headers, timeout=self.timeout)
        if status != 200:
            raise TransportError(f"MCP server returned HTTP {status}", status=status, body=body)
        message = _parse_sse_or_json(body)
        if "error" in message:
            err = message["error"]
            raise MCPError(f"MCP error {err.get('code')}: {err.get('message')}")
        if "result" not in message:
            raise MCPError(f"MCP response missing result: {message!r}")
        return message["result"]

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            raise MCPError(f"tool {name!r} failed: {_content_text(result)}")
        return _content_json(result)


def _parse_sse_or_json(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        raise MCPError(f"unexpected MCP response type: {type(body).__name__}")
    text = body.strip()
    if text.startswith("{"):
        return json.loads(text)
    # SSE: 取最后一个 data: 行
    data_lines = [
        line[len("data:"):].strip()
        for line in text.splitlines()
        if line.strip().startswith("data:")
    ]
    if not data_lines:
        raise MCPError(f"MCP response is neither JSON nor SSE: {text[:200]}")
    return json.loads(data_lines[-1])


def _content_text(result: dict[str, Any]) -> str:
    parts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    return "\n".join(parts)


def _content_json(result: dict[str, Any]) -> Any:
    text = _content_text(result)
    if not text:
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class DatasourceAdapters:
    """把多个数据源适配器聚合成 ToolGateway 内置工具所需的鸭子类型接口。"""

    def __init__(
        self,
        *,
        prometheus: PrometheusAdapter | None = None,
        loki: LokiAdapter | None = None,
        tempo: TempoAdapter | None = None,
    ) -> None:
        self.prometheus = prometheus
        self.loki = loki
        self.tempo = tempo

    def _need(self, adapter: Any, name: str) -> Any:
        if adapter is None:
            raise TransportError(f"{name} adapter is not configured")
        return adapter

    def query_metric_series(self, service: str, metric: str, start: str, end: str) -> Any:
        prom = self._need(self.prometheus, "prometheus")
        # 裸指标名 → 带 service 选择器；含 PromQL 语法符号则原样使用
        expression = metric if any(ch in metric for ch in "()[]{}") else f'{metric}{{service="{service}"}}'
        return prom.query_range(expression, start, end)

    def query_service_error_rate(self, service: str, start: str, end: str) -> Any:
        return self._need(self.prometheus, "prometheus").service_error_rate(service, start, end)

    def query_service_logs(
        self, service: str, start: str, end: str, filter: str | None = None
    ) -> Any:
        return self._need(self.loki, "loki").service_logs(service, start, end, filter=filter)

    def query_service_traces(
        self,
        service: str,
        start: str,
        end: str,
        min_duration_ms: int | None = None,
    ) -> Any:
        return self._need(self.tempo, "tempo").service_traces(
            service, start, end, min_duration_ms=min_duration_ms
        )


class MCPDatasourceAdapters(DatasourceAdapters):
    """通过 Grafana MCP Server 提供四个内置工具（生产推荐：不直连各数据源）。"""

    def __init__(
        self,
        client: GrafanaMCPClient,
        *,
        prometheus_uid: str = "prometheus",
        loki_uid: str = "loki",
        tempo_uid: str = "tempo",
    ) -> None:
        super().__init__()
        self.client = client
        self.prometheus_uid = prometheus_uid
        self.loki_uid = loki_uid
        self.tempo_uid = tempo_uid
        # 可选的 ds/query 降级路径：当 get_tempo_trace 不可用（Tempo 缺
        # /api/v2/traces）时，改走 Grafana /api/ds/query 取完整 span 树。
        # 实测该路径在 Grafana 11.1 + Tempo 2.5 下可用，不依赖 Tempo 版本。
        self.trace_fallback = None

    def query_metric_series(self, service: str, metric: str, start: str, end: str) -> Any:
        expression = metric if any(ch in metric for ch in "()[]{}") else f'{metric}{{service="{service}"}}'
        return self._query_prometheus(expression, start, end)

    def query_service_error_rate(self, service: str, start: str, end: str) -> Any:
        total = f'sum(rate(http_requests_total{{service="{service}"}}[1m]))'
        errors = f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[1m]))'
        return self._query_prometheus(f"{errors} / clamp_min({total}, 0.000001)", start, end)

    def _query_prometheus(self, expression: str, start: str, end: str) -> Any:
        """查询 Prometheus，并把响应还原成原生形状。

        两个实测结论（都不是文档写的）：

        1. **参数名**：`query_prometheus` 的真实签名是
           datasourceUid / expr / startTime / endTime / stepSeconds / queryType，
           **没有** startRFC3339 / endRFC3339。服务端对未知参数直接拒绝：
             unknown arguments "endRFC3339", "startRFC3339"
        2. **必须声明 queryType**：不给 queryType 时带 stepSeconds 的 range 查询
           不成立，而 `endTime` 是必填项。

        **响应形状**：MCP 返回 `{data: [...], exploreUrl}`，而不是 Prometheus 的
        `{status, data: {resultType, result}}`。`MetricNormalizer` 只认后者，
        不转换就会 ValueError —— 且会被 `_collect_node` 当成 missing_data 静默吞掉。
        """
        raw = self.client.call_tool(
            "query_prometheus",
            {
                "datasourceUid": self.prometheus_uid,
                "expr": expression,
                "startTime": _mcp_time(start),
                "endTime": _mcp_time(end),
                "queryType": "range",
                "stepSeconds": 60,
            },
        )
        return _mcp_prometheus_to_native(raw)

    def query_service_logs(self, service: str, start: str, end: str, filter: str | None = None) -> Any:
        logql = f'{{service="{service}"}}'
        if filter:
            logql += f' |= "{filter}"'
        raw = self.client.call_tool(
            "query_loki_logs",
            {
                "datasourceUid": self.loki_uid,
                "logql": logql,
                # 规范拼写是小写 fc（服务端 schema: startRfc3339 / endRfc3339）。
                # 写 startRFC3339 在 Go 的宽松解码下侥幸能过，换实现就会坏。
                "startRfc3339": _mcp_time(start),
                "endRfc3339": _mcp_time(end),
                "limit": 200,
            },
        )
        return _mcp_loki_to_native(raw)

    def query_service_traces(
        self, service: str, start: str, end: str, min_duration_ms: int | None = None
    ) -> Any:
        found = self.client.call_tool(
            # 参数名以**实测**为准，不是文档：mcp-grafana v2.1.1 的
            # search_tempo_traces 接受 datasourceUid / query / start / end，
            # 没有 q、startRFC3339、limit；get_tempo_trace 用 trace_id。
            # 写错误名会被服务端明确拒绝（"unknown arguments"），所以这里必须准确。
            "search_tempo_traces",
            {
                "datasourceUid": self.tempo_uid,
                "query": f'{{ resource.service.name = "{service}" }}',
                "start": start,
                "end": end,
            },
        )
        traces = found.get("traces", []) if isinstance(found, dict) else []
        batches: list[Any] = []
        degraded = False
        degraded_reason = ""
        for t in traces:
            trace_id = t.get("traceID") or t.get("traceId")
            if not trace_id:
                continue
            # Tempo 返回的 traceID 可能是短形式；存储的是 32 位十六进制
            if len(trace_id) < 32:
                trace_id = trace_id.rjust(32, "0")
            # **版本兼容**：mcp-grafana 的两个版本签名不同 ——
            #   v2.1.1（uvx）：允许可选 start/end（窄化时间范围，查得更快）
            #   Docker latest：只有 datasourceUid/trace_id/focus_span_id
            # 传 start/end 给旧版会被拒（unknown arguments）。因此先不带时间范围，
            # 失败且错误信息表明"unknown arguments"时再降级重试。
            try:
                payload = self._call_get_trace(trace_id, start=start, end=end)
            except Exception as exc:
                # get_tempo_trace 在该版本组合下可能恒 404（实测：
                # Grafana 11.1.0 + Tempo 2.5.0 走 /api/v2/traces 而 Tempo 2.5
                # 没有该端点）。降级顺序是**先优后劣**：
                #   1. ds/query 取完整 span 树（不依赖 Tempo 版本，最优）
                #   2. search 的 spanSet 摘要（一定会丢父子结构，明确标 degraded）
                if self.trace_fallback is not None:
                    try:
                        full = self.trace_fallback.get_trace_batches(trace_id)
                    except Exception:
                        full = []
                    if full:
                        batches.extend(full)
                        continue
                fallback = _mcp_tempo_search_to_batches(t)
                if fallback:
                    batches.extend(fallback)
                    degraded = True
                    degraded_reason = f"get_tempo_trace unavailable: {str(exc)[:160]}"
                    continue
                # 没有 spanSet 可降级时必须抛出：静默 continue 会让证据无声消失，
                # 上层无法区分"这个 trace 没数据"和"我们没取回来"。
                # 抛出去由 `_collect_node` 记入 missing_data，才是诚实的失败路径。
                raise
            if isinstance(payload, dict):
                batches.extend(_mcp_tempo_trace_to_batches(payload))
        result: dict[str, Any] = {
            "traces_considered": len(traces),
            "batches": batches,
            "degraded": degraded,
        }
        if degraded:
            # 必须可辨：只含摘要 span 的结果不能被误当成完整 span 树
            result["degraded_reason"] = degraded_reason
        return result

    def with_trace_fallback(self, fallback) -> "MCPDatasourceAdapters":
        """挂载 ds/query 降级路径（返回 self 以便链式配置）。"""
        self.trace_fallback = fallback
        return self

    def _call_get_trace(self, trace_id: str, *, start: str | None, end: str | None) -> Any:
        """调用 get_tempo_trace，容忍两个 mcp-grafana 版本的参数差异。

        旧版（Docker latest）没有 start/end；新版（v2.1.1+）有。
        策略：先带时间范围（新版能查得更快、更准），若服务端明确报
        "unknown arguments" 则降级为不带时间范围重试。

        只在错误信息明确是参数不被接受时才降级 —— 其他错误（如 404）必须
        原样上报，否则会把"trace 真的不存在"误当成"参数不支持"。
        """
        args: dict[str, Any] = {"datasourceUid": self.tempo_uid, "trace_id": trace_id}
        if start and end:
            args["start"] = start
            args["end"] = end
        try:
            return self.client.call_tool("get_tempo_trace", args)
        except Exception as exc:
            message = str(exc)
            if start and end and "unknown argument" in message.lower():
                return self.client.call_tool(
                    "get_tempo_trace",
                    {"datasourceUid": self.tempo_uid, "trace_id": trace_id},
                )
            raise
