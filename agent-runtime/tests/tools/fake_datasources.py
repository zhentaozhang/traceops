"""假数据源服务器（标准库 http.server），供适配器做真实 HTTP 往返测试。

为什么需要它：
现有 `test_adapters.py` 用 `FakeTransport` 只验证「请求构造 + 响应解析」的
内存逻辑；`UrllibTransport` 真正发出 HTTP 时，URL 路径、query 编码、
epoch 秒 vs 纳秒、`Content-Type`/SSE 解码都可能出问题，而假桩全部绕过。
这里的服务器**真的从 HTTP 请求里解析 query string**，并按收到的参数生成
响应（时间戳来自收到的 start/step，标签来自收到的 PromQL/LogQL），因此
「适配器把参数传对了」是可以被断言的，而不是靠固定响应自证。

设计要点：
- 每个 handler 记录收到的每个请求（方法/路径/query/头/请求体/响应状态），
  测试据此断言「服务器端收到了什么」。
- 缺少必需参数时按真实服务语义返回 4xx，而不是默默返回固定数据 ——
  参数名写错或单位写错会立刻暴露。
- `serve()` 起服务在随机端口，退出时 shutdown + server_close + join，
  避免线程泄漏让 pytest 卡住。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator


@dataclass
class Request:
    """服务器侧记录的一次请求。"""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any = None
    response_status: int | None = None
    response_content_type: str | None = None


@dataclass
class FakeServer:
    """`serve()` 的产物：base_url + 服务器收到的请求列表。

    比只给 base_url 多一个 `requests`，否则测试无法断言「服务器收到了什么」。
    """

    base_url: str
    requests: list[Request] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [r.path for r in self.requests]

    def only(self, path: str) -> Request:
        """取唯一一条指定路径的请求；不唯一时直接失败（避免静默选错）。"""
        matched = [r for r in self.requests if r.path == path]
        assert len(matched) == 1, f"expected exactly 1 request to {path}, got paths={self.paths()}"
        return matched[0]


def _parse_query(query_string: str) -> dict[str, str]:
    # keep_blank_values=True：空值参数也算「传了」，便于区分"没传"和"传了空串"
    parsed = urllib.parse.parse_qs(query_string, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


class _BaseFakeHandler(BaseHTTPRequestHandler):
    """共用：记录请求、按路径分派、异常兜底。

    子类只需要实现 `_handle_get` / `_handle_post`。
    子类上声明的 `records` / `config` 在 `serve()` 里被绑定成子类的属性，
    因此每个服务器实例都有独立的记录与配置（不跨测试串味）。
    """

    records: list[Request] = []
    config: dict[str, Any] = {}

    # 故意保持 HTTP/1.0：无 keep-alive，响应必带 Content-Length，
    # 不会因为连接复用让测试卡住。
    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 约定)
        self._dispatch(self._handle_get)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self._dispatch(self._handle_post, raw)

    def _dispatch(self, handler, raw: bytes | None = None) -> None:
        record = self._make_record(raw)
        try:
            handler(record, raw)
        except Exception as exc:  # noqa: BLE001
            # 处理异常时也要回响应，否则客户端会读到空 body 并挂住
            self._send_json(record, 500, {"error": f"fake server crashed: {exc!r}"})

    def _make_record(self, raw: bytes | None) -> Request:
        parts = urllib.parse.urlsplit(self.path)
        body: Any = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = raw.decode("utf-8", errors="replace")
        record = Request(
            method=self.command,
            path=parts.path,
            query=_parse_query(parts.query),
            headers=dict(self.headers.items()),
            body=body,
        )
        type(self).records.append(record)
        return record

    # ---- 响应写出 ----
    def _send(
        self,
        record: Request,
        status: int,
        payload: bytes,
        content_type: str = "application/json",
    ) -> None:
        record.response_status = status
        record.response_content_type = content_type
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, record: Request, status: int, payload: Any) -> None:
        self._send(record, status, json.dumps(payload).encode("utf-8"))

    def _handle_get(self, record: Request, raw: bytes | None) -> None:
        raise NotImplementedError

    def _handle_post(self, record: Request, raw: bytes | None) -> None:
        raise NotImplementedError

    def log_message(self, *args: Any) -> None:  # 静音，避免污染 pytest 输出
        pass


# --------------------------------------------------------------------------- #
# Prometheus
# --------------------------------------------------------------------------- #

_SELECTOR_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\{')
_SERVICE_RE = re.compile(r'service="([^"]*)"')
_5XX_RE = re.compile(r'status=~"5\.\."')


class FakePrometheusHandler(_BaseFakeHandler):
    """`/api/v1/query_range` + `/api/v1/query`。

    config:
      failure: None | "http_503" | "error_status"
    """

    config: dict[str, Any] = {"failure": None}

    def _handle_get(self, record: Request, raw: bytes | None) -> None:
        failure = self.config.get("failure")
        if failure == "http_503":
            self._send_json(record, 503, {"status": "error", "error": "service unavailable"})
            return
        if failure == "error_status":
            # HTTP 200 + {"status":"error"}：Prometheus 对查询语法错误的真实返回方式
            self._send_json(record, 200, {"status": "error", "error": "query parse error: unexpected end of input"})
            return

        if record.path == "/api/v1/query_range":
            self._query_range(record)
        elif record.path == "/api/v1/query":
            self._query_instant(record)
        else:
            # 路径写错要显式暴露，不能悄悄给 200
            self._send_json(record, 404, {"status": "error", "error": f"unsupported path: {record.path}"})

    # ---- 参数校验：按 Prometheus 语义返回 400 ----
    def _require(self, record: Request, name: str) -> str | None:
        value = record.query.get(name)
        if value is None:
            self._send_json(
                record, 400, {"status": "error", "error": f'missing required parameter "{name}"'}
            )
            return None
        return value

    def _check_epoch_seconds(self, record: Request, name: str) -> float | None:
        raw_value = self._require(record, name)
        if raw_value is None:
            return None
        try:
            seconds = float(raw_value)
        except ValueError:
            self._send_json(
                record, 400, {"status": "error", "error": f'invalid parameter "{name}": cannot parse "{raw_value}"'}
            )
            return None
        # 单位守卫：毫秒/微秒/纳秒在这里会被拒绝，而不是被当成合法的秒
        if not 0 <= seconds < 1e12:
            self._send_json(
                record,
                400,
                {"status": "error", "error": f'invalid parameter "{name}": {raw_value} is not unix epoch seconds'},
            )
            return None
        return seconds

    def _query_range(self, record: Request) -> None:
        query = self._require(record, "query")
        if query is None:
            return
        start = self._check_epoch_seconds(record, "start")
        if start is None:
            return
        end = self._check_epoch_seconds(record, "end")
        if end is None:
            return
        step_raw = self._require(record, "step")
        if step_raw is None:
            return
        try:
            step = int(float(step_raw))
        except ValueError:
            self._send_json(record, 400, {"status": "error", "error": f'invalid parameter "step": {step_raw}'})
            return
        if step <= 0:
            self._send_json(record, 400, {"status": "error", "error": 'invalid parameter "step": must be positive'})
            return

        # 响应完全由收到的参数生成：采样时间戳就是 start + i*step，
        # 标签来自 PromQL 里的选择器 —— 参数没传到，响应就明显不对。
        series = self._series_for(query, start, end, step)
        self._send_json(record, 200, {"status": "success", "data": {"resultType": "matrix", "result": series}})

    def _query_instant(self, record: Request) -> None:
        query = self._require(record, "query")
        if query is None:
            return
        when_raw = record.query.get("time")
        if when_raw is not None:
            when = self._check_epoch_seconds(record, "time")
            if when is None:
                return
        else:
            when = 0.0
        metric = self._metric_labels(query)
        self._send_json(
            record,
            200,
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": metric, "value": [when, "1.0000"]}],
                },
            },
        )

    def _metric_labels(self, query: str) -> dict[str, str]:
        match = _SELECTOR_RE.search(query)
        name = match.group(1) if match else "unknown_metric"
        labels = {"__name__": name}
        service = _SERVICE_RE.search(query)
        if service:
            labels["service"] = service.group(1)
        return labels

    def _series_for(self, query: str, start: float, end: float, step: int) -> list[dict[str, Any]]:
        # 5xx 错误率表达式是两个序列相除，值域 0..1；普通计数器是整数
        value = "0.4200" if _5XX_RE.search(query) or " / " in query else "1234.0000"
        point_count = max(1, min(20, int((end - start) // step) + 1))
        values = [[start + index * step, value] for index in range(point_count)]
        return [{"metric": self._metric_labels(query), "values": values}]


# --------------------------------------------------------------------------- #
# Loki
# --------------------------------------------------------------------------- #

_LOKI_SERVICE_RE = re.compile(r'\{\s*service="([^"]*)"\s*\}')
_LOKI_FILTER_RE = re.compile(r'\|=\s*"([^"]*)"')


class FakeLokiHandler(_BaseFakeHandler):
    """`/loki/api/v1/query_range`。config: failure = None | "http_500"。"""

    config: dict[str, Any] = {"failure": None}

    def _handle_get(self, record: Request, raw: bytes | None) -> None:
        if self.config.get("failure") == "http_500":
            # 文本 body：验证适配器对非 JSON 错误体也不会崩
            self._send(
                record,
                500,
                b"internal server error: ingester unavailable",
                content_type="text/plain; charset=utf-8",
            )
            return
        if record.path != "/loki/api/v1/query_range":
            self._send_json(record, 404, {"status": "error", "error": f"unsupported path: {record.path}"})
            return

        query = record.query.get("query")
        if query is None:
            self._send_json(record, 400, {"status": "error", "error": 'missing required parameter "query"'})
            return
        # Loki 的 start/end 必须是纳秒整数字符串：位数不对直接 400
        for name in ("start", "end"):
            value = record.query.get(name)
            if value is None:
                self._send_json(record, 400, {"status": "error", "error": f'missing required parameter "{name}"'})
                return
            if not value.isdigit() or len(value) < 16:
                self._send_json(
                    record,
                    400,
                    {
                        "status": "error",
                        "error": f'invalid parameter "{name}": expected unix nanoseconds, got "{value}"',
                    },
                )
                return
        if record.query.get("limit") is None:
            self._send_json(record, 400, {"status": "error", "error": 'missing required parameter "limit"'})
            return
        if record.query.get("direction") is None:
            self._send_json(record, 400, {"status": "error", "error": 'missing required parameter "direction"'})
            return
        if int(record.query["start"]) > int(record.query["end"]):
            self._send_json(record, 400, {"status": "error", "error": 'invalid query: start is after end'})
            return

        service_match = _LOKI_SERVICE_RE.search(query)
        filter_match = _LOKI_FILTER_RE.search(query)
        # 时间戳直接取收到的 start（纳秒），因此能证明 start 真的被用了
        timestamp_ns = record.query["start"]
        message = "upstream request failed"
        if filter_match:
            message = f"redis {filter_match.group(1)}: context deadline exceeded"
        self._send_json(
            record,
            200,
            {
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {
                                "service": service_match.group(1) if service_match else "unknown",
                                "level": "error",
                            },
                            "values": [[timestamp_ns, message]],
                        }
                    ],
                },
            },
        )


# --------------------------------------------------------------------------- #
# Tempo
# --------------------------------------------------------------------------- #

_TEMPO_TRACE_PATH_RE = re.compile(r"^/api/traces/([0-9A-Za-z]+)$")


class FakeTempoHandler(_BaseFakeHandler):
    """`/api/search` + `/api/traces/{id}`。

    config:
      trace_status: None | int(例如 404)，强制 trace-by-id 失败
    说明：`/api/traces/{未知 id}` 天然返回 404，这比所有请求都失败更接近真实行为。
    """

    config: dict[str, Any] = {"trace_status": None}

    def _handle_get(self, record: Request, raw: bytes | None) -> None:
        if record.path == "/api/search":
            self._search(record)
        elif _TEMPO_TRACE_PATH_RE.match(record.path):
            self._trace(record)
        else:
            self._send_json(record, 404, {"error": f"unsupported path: {record.path}"})

    def _search(self, record: Request) -> None:
        tags = record.query.get("tags")
        if tags is None:
            self._send_json(record, 400, {"error": 'missing required parameter "tags"'})
            return
        for name in ("start", "end"):
            value = record.query.get(name)
            if value is None:
                self._send_json(record, 400, {"error": f'missing required parameter "{name}"'})
                return
            # Tempo 的 search 用 epoch 秒；纳秒会超过 1e12
            if not value.isdigit() or int(value) >= 10**12:
                self._send_json(
                    record, 400, {"error": f'invalid parameter "{name}": expected unix epoch seconds, got "{value}"'}
                )
                return
        if record.query.get("limit") is None:
            self._send_json(record, 400, {"error": 'missing required parameter "limit"'})
            return

        service = tags.split("=", 1)[1] if "=" in tags else "unknown"
        limit = int(record.query["limit"])
        count = max(1, min(limit, 2))
        start_ns = int(record.query["start"]) * 1_000_000_000
        traces = []
        for index in range(count):
            trace_id = hashlib.md5(f"{service}-{index}".encode()).hexdigest()
            self.server.traces[trace_id] = {  # type: ignore[attr-defined]
                "service": service,
                "start_ns": start_ns + index * 1_000_000_000,
            }
            traces.append(
                {
                    "traceID": trace_id,
                    "rootServiceName": service,
                    "rootTraceName": "GET /orders",
                    "startTimeUnixNano": str(start_ns + index * 1_000_000_000),
                    "durationMs": 900 + index * 300,
                }
            )
        self._send_json(record, 200, {"traces": traces, "metrics": {"totalBlocks": 1}})

    def _trace(self, record: Request) -> None:
        trace_id = _TEMPO_TRACE_PATH_RE.match(record.path).group(1)  # type: ignore[union-attr]
        forced = self.config.get("trace_status")
        if forced is not None:
            self._send_json(record, forced, {"error": f"trace {trace_id} unavailable"})
            return
        info = self.server.traces.get(trace_id)  # type: ignore[attr-defined]
        if info is None:
            self._send_json(record, 404, {"error": f"trace {trace_id} not found"})
            return
        # 真实 Tempo 的 /api/traces 返回 OTLP 风格 JSON（resource → scopeSpans → spans）
        start_ns = info["start_ns"]
        self._send_json(
            record,
            200,
            {
                "batches": [
                    {
                        "resource": {
                            "attributes": [
                                {"key": "service.name", "value": {"stringValue": info["service"]}}
                            ]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": trace_id,
                                        "spanId": "1111111111111111",
                                        "name": "GET /orders",
                                        "startTimeUnixNano": str(start_ns),
                                        "endTimeUnixNano": str(start_ns + 900_000_000),
                                    }
                                ]
                            }
                        ],
                    }
                ]
            },
        )


# --------------------------------------------------------------------------- #
# Grafana MCP (JSON-RPC over streamable HTTP)
# --------------------------------------------------------------------------- #

_MCP_KNOWN_TOOLS = {"query_prometheus", "query_loki_logs", "search_tempo_traces", "get_tempo_trace"}


class FakeGrafanaMCPHandler(_BaseFakeHandler):
    """POST JSON-RPC。config:

      response_mode: "json" | "sse"
      tool_error: bool     —— result.isError = true 的失败工具调用
      rpc_error: dict | None —— 直接返回 JSON-RPC error 对象

    按真实 Streamable HTTP 语义校验 Content-Type 与 Accept：
    客户端不发对头就会拿到 415/406，从而验证 `UrllibTransport` 的头是对的。
    """

    config: dict[str, Any] = {"response_mode": "json", "tool_error": False, "rpc_error": None}

    def _handle_post(self, record: Request, raw: bytes | None) -> None:
        content_type = (record.headers.get("Content-Type") or "").lower()
        if "application/json" not in content_type:
            self._send_json(record, 415, {"error": "Content-Type must be application/json"})
            return
        accept = (record.headers.get("Accept") or "").lower()
        # 真实 streamable HTTP 客户端必须同时接受 JSON 与 SSE，否则服务端无法选择传输方式
        if "text/event-stream" not in accept:
            self._send_json(record, 406, {"error": "Accept must include text/event-stream"})
            return

        if not isinstance(record.body, dict):
            self._rpc_reply(
                record, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            )
            return
        message = record.body
        if message.get("jsonrpc") != "2.0" or "method" not in message:
            self._rpc_reply(
                record,
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32600, "message": "invalid request"},
                },
            )
            return

        configured_error = self.config.get("rpc_error")
        if configured_error is not None:
            self._rpc_reply(record, {"jsonrpc": "2.0", "id": message.get("id"), "error": configured_error})
            return

        method = message["method"]
        params = message.get("params") or {}
        if method == "tools/call":
            reply = self._tools_call(message.get("id"), params)
        elif method == "tools/list":
            reply = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {"tools": [{"name": name} for name in sorted(_MCP_KNOWN_TOOLS)]},
            }
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32601, "message": f"unknown method {method!r}"},
            }
        self._rpc_reply(record, reply)

    def _tools_call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not name:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "invalid params: missing name"},
            }
        if name not in _MCP_KNOWN_TOOLS:
            # 未知工具名 → JSON-RPC error，与真实 MCP server 一致
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown tool {name!r}"},
            }
        if self.config.get("tool_error"):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "upstream datasource unreachable: connection refused"}],
                    "isError": True,
                },
            }
        arguments = params.get("arguments") or {}
        # 把收到的参数原样回显：客户端能断言「服务器确实收到了这些参数」
        payload = {"server": "fake-grafana-mcp", "tool": name, "echo": arguments}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False},
        }

    def _rpc_reply(self, record: Request, reply: dict[str, Any]) -> None:
        if self.config.get("response_mode") == "sse":
            body = f"event: message\ndata: {json.dumps(reply)}\n\n".encode("utf-8")
            self._send(record, 200, body, content_type="text/event-stream")
        else:
            self._send_json(record, 200, reply)


# --------------------------------------------------------------------------- #
# 启动/停止
# --------------------------------------------------------------------------- #


@contextmanager
def serve(handler_class: type[_BaseFakeHandler], **config: Any) -> Iterator[FakeServer]:
    """在随机端口起一个假数据源，退出时可靠关闭。

    为什么用动态子类：`records`/`config` 挂在类上，如果直接用同一个类反复起
    服务，多个服务器会共享记录与配置（并发/重复测试时串味）。为每个服务器
    生成一个子类，得到独立的类属性。
    """
    bound = type(
        f"{handler_class.__name__}__bound",
        (handler_class,),
        {"records": [], "config": {**handler_class.config, **config}},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), bound)
    server.daemon_threads = True  # 防止 handler 线程阻塞进程退出
    server.traces = {}  # type: ignore[attr-defined]  # Tempo 用：已知 trace 集合
    thread = threading.Thread(target=server.serve_forever, name="fake-datasource", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield FakeServer(base_url=base_url, requests=bound.records)
    finally:
        server.shutdown()      # 停止 serve_forever 循环
        server.server_close()  # 释放监听端口
        thread.join(timeout=5) # 确保线程退出，pytest 不会挂着
