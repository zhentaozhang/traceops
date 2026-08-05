"""数据源适配器测试（Cycle A）。

用 FakeTransport 模拟 HTTP 层，验证：
- Prometheus/Loki/Tempo 适配器的请求构造与响应解析
- MCP JSON-RPC 客户端（含 SSE 响应解析、错误处理）
- DatasourceAdapters 与 ToolGateway 的端到端接线
- UrllibTransport 真实网络路径（本地 http.server）
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from traceops.tools.adapters import (
    DatasourceAdapters,
    GrafanaMCPClient,
    LokiAdapter,
    MCPError,
    PrometheusAdapter,
    TempoAdapter,
    TransportError,
    UrllibTransport,
)
from traceops.tools.gateway import ToolGateway, register_builtin_tools
from traceops.runtime.budget import BudgetManager

START = "2026-10-30T10:00:00Z"
END = "2026-10-30T10:05:00Z"


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: list[tuple[int, object]] = []

    def queue(self, status: int, payload: object) -> None:
        self.responses.append((status, payload))

    def get(self, url, params=None, headers=None, timeout=10.0):
        self.calls.append({"method": "GET", "url": url, "params": params, "headers": headers})
        return self.responses.pop(0)

    def post(self, url, payload, headers=None, timeout=10.0):
        self.calls.append({"method": "POST", "url": url, "payload": payload, "headers": headers})
        return self.responses.pop(0)


PROM_RANGE = {
    "status": "success",
    "data": {
        "resultType": "matrix",
        "result": [
            {
                "metric": {"service": "order-service"},
                "values": [[1730282400.0, "0.01"], [1730282460.0, "0.42"]],
            }
        ],
    },
}

LOKI_RANGE = {
    "status": "success",
    "data": {
        "resultType": "streams",
        "result": [
            {
                "stream": {"service": "order-service", "level": "error"},
                "values": [["1730282400000000000", "redis timeout"]],
            }
        ],
    },
}

TEMPO_SEARCH = {
    "traces": [
        {"traceID": "a" * 32, "rootServiceName": "order-service", "durationMs": 900},
        {"traceID": "b" * 32, "rootServiceName": "order-service", "durationMs": 1200},
    ]
}
TEMPO_TRACE = {
    "batches": [
        {
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "order-service"}}]},
            "scopeSpans": [
                {
                    "spans": [
                        {
                            "traceId": "a" * 32,
                            "spanId": "1111111111111111",
                            "name": "GET /orders",
                            "startTimeUnixNano": "1730282400000000000",
                            "endTimeUnixNano": "1730282400900000000",
                        }
                    ]
                }
            ],
        }
    ]
}


# ---- Prometheus ----

def test_prometheus_query_range_builds_request_and_returns_body() -> None:
    transport = FakeTransport()
    transport.queue(200, PROM_RANGE)
    adapter = PrometheusAdapter("http://prom:9090", transport=transport)

    body = adapter.query_range("rate(http_requests_total[1m])", START, END, step_seconds=30)

    assert body == PROM_RANGE
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "http://prom:9090/api/v1/query_range"
    assert call["params"]["query"] == "rate(http_requests_total[1m])"
    assert call["params"]["step"] == 30
    from datetime import datetime
    assert float(call["params"]["start"]) == datetime.fromisoformat(START.replace("Z", "+00:00")).timestamp()
    assert float(call["params"]["end"]) == datetime.fromisoformat(END.replace("Z", "+00:00")).timestamp()


def test_prometheus_raises_transport_error_on_non_200() -> None:
    transport = FakeTransport()
    transport.queue(503, {"error": "unavailable"})
    adapter = PrometheusAdapter("http://prom:9090", transport=transport)

    with pytest.raises(TransportError, match="503"):
        adapter.query_range("up", START, END)


def test_prometheus_raises_transport_error_on_error_status_body() -> None:
    transport = FakeTransport()
    transport.queue(200, {"status": "error", "error": "parse error"})
    adapter = PrometheusAdapter("http://prom:9090", transport=transport)

    with pytest.raises(TransportError, match="parse error"):
        adapter.query_instant("bad{")


def test_prometheus_service_error_rate_uses_5xx_expression() -> None:
    transport = FakeTransport()
    transport.queue(200, PROM_RANGE)
    adapter = PrometheusAdapter("http://prom:9090", transport=transport)

    adapter.service_error_rate("order-service", START, END)

    query = transport.calls[0]["params"]["query"]
    assert 'status=~"5.."' in query
    assert 'service="order-service"' in query


# ---- Loki ----

def test_loki_service_logs_builds_logql_with_filter() -> None:
    transport = FakeTransport()
    transport.queue(200, LOKI_RANGE)
    adapter = LokiAdapter("http://loki:3100", transport=transport)

    body = adapter.service_logs("order-service", START, END, filter="redis")

    assert body == LOKI_RANGE
    call = transport.calls[0]
    assert call["url"] == "http://loki:3100/loki/api/v1/query_range"
    assert '{service="order-service"}' in call["params"]["query"]
    assert '|= "redis"' in call["params"]["query"]
    assert call["params"]["limit"] == 200


# ---- Tempo ----

def test_tempo_service_traces_searches_then_fetches_each_trace() -> None:
    transport = FakeTransport()
    transport.queue(200, TEMPO_SEARCH)
    transport.queue(200, TEMPO_TRACE)
    transport.queue(200, TEMPO_TRACE)
    adapter = TempoAdapter("http://tempo:3200", transport=transport)

    body = adapter.service_traces("order-service", START, END, min_duration_ms=500)

    assert body["traces_considered"] == 2
    assert len(body["batches"]) == 2  # one batch per trace payload
    search_call, trace_call_1, trace_call_2 = transport.calls
    assert search_call["url"] == "http://tempo:3200/api/search"
    assert trace_call_1["url"] == f"http://tempo:3200/api/traces/{'a' * 32}"
    assert trace_call_2["url"] == f"http://tempo:3200/api/traces/{'b' * 32}"


def test_tempo_get_trace_raises_on_transport_error() -> None:
    transport = FakeTransport()
    transport.queue(404, {"message": "not found"})
    adapter = TempoAdapter("http://tempo:3200", transport=transport)

    with pytest.raises(TransportError, match="404"):
        adapter.get_trace("c" * 32)


# ---- Grafana MCP client ----

def _mcp_ok(text: str) -> tuple[int, dict]:
    return 200, {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


def test_mcp_client_call_tool_posts_jsonrpc_and_parses_text_result() -> None:
    transport = FakeTransport()
    transport.queue(*_mcp_ok(json.dumps({"data": {"resultType": "vector", "result": []}})))
    client = GrafanaMCPClient("http://grafana:3000/mcp", transport=transport)

    result = client.call_tool("query_prometheus", {"expr": "up", "datasourceUid": "prom-1"})

    assert result == {"data": {"resultType": "vector", "result": []}}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["payload"]["method"] == "tools/call"
    assert call["payload"]["params"]["name"] == "query_prometheus"
    assert call["payload"]["params"]["arguments"]["expr"] == "up"


def test_mcp_client_parses_sse_response() -> None:
    transport = FakeTransport()
    sse_body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\\"ok\\":true}"}],"isError":false}}\n\n'
    # SSE 由 UrllibTransport 解码为文本；FakeTransport 直接返回文本
    transport.queue(200, sse_body)
    client = GrafanaMCPClient("http://grafana:3000/mcp", transport=transport)

    result = client.call_tool("query_loki_logs", {"logql": "{}"})

    assert result == {"ok": True}


def test_mcp_client_raises_on_error_object() -> None:
    transport = FakeTransport()
    transport.queue(200, {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "unknown tool"}})
    client = GrafanaMCPClient("http://grafana:3000/mcp", transport=transport)

    with pytest.raises(MCPError, match="unknown tool"):
        client.call_tool("nope", {})


def test_mcp_client_raises_when_tool_result_is_error() -> None:
    transport = FakeTransport()
    transport.queue(
        200,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "boom: datasource unreachable"}], "isError": True},
        },
    )
    client = GrafanaMCPClient("http://grafana:3000/mcp", transport=transport)

    with pytest.raises(MCPError, match="datasource unreachable"):
        client.call_tool("query_prometheus", {})


# ---- DatasourceAdapters 与 Gateway 接线 ----

def test_datasource_adapters_serve_gateway_builtin_tools() -> None:
    prom_transport = FakeTransport()
    prom_transport.queue(200, PROM_RANGE)
    loki_transport = FakeTransport()
    loki_transport.queue(200, LOKI_RANGE)
    tempo_transport = FakeTransport()
    tempo_transport.queue(200, TEMPO_SEARCH)
    tempo_transport.queue(200, TEMPO_TRACE)
    tempo_transport.queue(200, TEMPO_TRACE)

    adapters = DatasourceAdapters(
        prometheus=PrometheusAdapter("http://prom:9090", transport=prom_transport),
        loki=LokiAdapter("http://loki:3100", transport=loki_transport),
        tempo=TempoAdapter("http://tempo:3200", transport=tempo_transport),
    )
    gw = ToolGateway()
    register_builtin_tools(gw, adapters)
    budget = BudgetManager()

    metric_result = gw.invoke(
        "query_metric_series",
        {"service": "order-service", "metric": "http_requests_total", "start": START, "end": END},
        budget=budget,
    )
    log_result = gw.invoke(
        "query_service_logs",
        {"service": "order-service", "start": START, "end": END},
        budget=budget,
    )
    trace_result = gw.invoke(
        "query_service_traces",
        {"service": "order-service", "start": START, "end": END},
        budget=budget,
    )

    assert metric_result == PROM_RANGE
    assert log_result == LOKI_RANGE
    assert trace_result["traces_considered"] == 2
    assert [c["outcome"] for c in gw.audit_log()] == ["OK", "OK", "OK"]


def test_datasource_adapters_error_rate_tool_works() -> None:
    prom_transport = FakeTransport()
    prom_transport.queue(200, PROM_RANGE)
    adapters = DatasourceAdapters(prometheus=PrometheusAdapter("http://prom:9090", transport=prom_transport))
    gw = ToolGateway()
    register_builtin_tools(gw, adapters)

    result = gw.invoke(
        "query_service_error_rate",
        {"service": "order-service", "start": START, "end": END},
        budget=BudgetManager(),
    )

    assert result == PROM_RANGE


# ---- UrllibTransport 真实网络路径 ----

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        payload = {"echo": json.loads(body)}
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):  # silence
        pass


def test_urllib_transport_get_and_post() -> None:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        transport = UrllibTransport()
        status, body = transport.get(f"{base}/api/v1/query?x=1")
        assert status == 200
        assert body == {"path": "/api/v1/query?x=1"}

        status, body = transport.post(f"{base}/mcp", {"hello": "world"})
        assert status == 200
        assert body == {"echo": {"hello": "world"}}
    finally:
        server.shutdown()
