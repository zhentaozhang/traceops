"""适配器真实 HTTP 往返测试（`test_adapters.py` 的补充，不是替代）。

为什么单独一个文件：
`test_adapters.py` 用 `FakeTransport` 验证「请求构造 + 响应解析」的内存逻辑，
但把 `UrllibTransport` 换进真实链路后才会暴露：URL 路径拼错、query 编码错、
epoch 秒/纳秒单位混用、Content-Type/SSE 解码失败、请求头缺失等问题。
这里全程走 `http.server` 搭的假数据源（`fake_datasources.py`），
每个断言都基于**服务器端真实收到的请求**或**由收到的参数生成的响应**。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from tests.tools.fake_datasources import (
    FakeGrafanaMCPHandler,
    FakeLokiHandler,
    FakePrometheusHandler,
    FakeTempoHandler,
    serve,
)
from traceops.runtime.budget import BudgetManager
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

START = "2026-10-30T10:00:00Z"
END = "2026-10-30T10:05:00Z"
START_EPOCH = 1793354400.0
END_EPOCH = 1793354700.0
START_NANOS = 1793354400000000000
END_NANOS = 1793354700000000000

# 用真实传输层：这是本文件的意义所在，任何测试都不应替换掉它
def _transport() -> UrllibTransport:
    return UrllibTransport()


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# --------------------------------------------------------------------------- #
# Prometheus
# --------------------------------------------------------------------------- #


def test_prometheus_query_range_hits_real_endpoint_with_epoch_seconds() -> None:
    """路径、参数名、时间单位都必须在真实 HTTP 层成立。

    断言服务器收到的 start/end 是 epoch **秒**（1e9 量级），而不是毫秒/微秒/纳秒；
    这类单位错误在 FakeTransport 下完全看不出来（假桩不解析参数）。
    """
    with serve(FakePrometheusHandler) as srv:
        adapter = PrometheusAdapter(srv.base_url, transport=_transport())
        body = adapter.query_range(
            'http_requests_total{service="order-service"}', START, END, step_seconds=60
        )

        record = srv.only("/api/v1/query_range")
        assert record.method == "GET"

        # 秒级 epoch：1793354400 落在 [1e9, 1e12) 区间，毫秒/微秒/纳秒都落不进去
        start = float(record.query["start"])
        end = float(record.query["end"])
        assert start == pytest.approx(_epoch(START), abs=1e-6)
        assert end == pytest.approx(_epoch(END), abs=1e-6)
        assert 1e9 <= start < 1e12 and 1e9 <= end < 1e12
        assert record.query["step"] == "60"
        assert record.query["query"] == 'http_requests_total{service="order-service"}'

        # 响应由收到的 start/step 生成：数据点时间戳 == start + i*step
        assert body["status"] == "success"
        assert body["data"]["resultType"] == "matrix"
        values = body["data"]["result"][0]["values"]
        assert [row[0] for row in values[:3]] == [start, start + 60, start + 120]
        assert body["data"]["result"][0]["metric"]["service"] == "order-service"


def test_prometheus_service_error_rate_expression_reaches_server() -> None:
    """`service_error_rate` 生成的 5xx 表达式必须原样出现在服务器收到的 query 里。

    只断言本地表达式字符串是不够的 —— 真正的风险是表达式在编码/拼装过程中被改动。
    同时服务器检测到 `status=~"5.."` 才返回 0.42（错误率量级），
    从而证明表达式确实参与了响应生成。
    """
    with serve(FakePrometheusHandler) as srv:
        adapter = PrometheusAdapter(srv.base_url, transport=_transport())
        body = adapter.service_error_rate("order-service", START, END)

        record = srv.only("/api/v1/query_range")
        received = record.query["query"]
        assert 'status=~"5.."' in received
        assert 'service="order-service"' in received
        assert "clamp_min" in received

        value = body["data"]["result"][0]["values"][0][1]
        assert float(value) < 1.0  # 错误率是 0..1 的比例，不是计数器原值


# --------------------------------------------------------------------------- #
# Loki
# --------------------------------------------------------------------------- #


def test_loki_service_logs_sends_nanosecond_start_end() -> None:
    """Loki 的 start/end 必须是纳秒字符串（19 位），秒或毫秒会被真实 Loki 拒绝。

    假数据源按 Loki 语义做位数校验（<16 位直接 400），
    因此这里拿到 200 本身就是"单位对了"的证据；再显式断言位数与数值。
    """
    with serve(FakeLokiHandler) as srv:
        adapter = LokiAdapter(srv.base_url, transport=_transport())
        body = adapter.service_logs("order-service", START, END)

        record = srv.only("/loki/api/v1/query_range")
        start_raw, end_raw = record.query["start"], record.query["end"]
        assert len(start_raw) == 19, f"expected nanos (19 digits), got {start_raw!r}"
        assert len(end_raw) == 19, f"expected nanos (19 digits), got {end_raw!r}"
        assert start_raw.isdigit() and end_raw.isdigit()
        assert int(start_raw) == int(_epoch(START)) * 10**9
        assert int(end_raw) == int(_epoch(END)) * 10**9
        assert record.query["direction"] == "backward"
        assert record.query["limit"] == "200"

        # 服务器直接把收到的 start（纳秒）回填进日志时间戳
        assert body["data"]["result"][0]["values"][0][0] == start_raw
        assert body["data"]["result"][0]["stream"]["service"] == "order-service"


def test_loki_service_logs_filter_reaches_server_as_logql() -> None:
    """filter 必须作为 LogQL 行过滤（`|= "..."`）真实送达，而不是只在客户端拼接。

    服务器只有在解析到 `|=` 时才把过滤词写进日志内容，所以断言消息文本
    能证明过滤条件参与了一次完整的 HTTP 往返。
    """
    with serve(FakeLokiHandler) as srv:
        adapter = LokiAdapter(srv.base_url, transport=_transport())
        body = adapter.service_logs("order-service", START, END, filter="timeout")

        record = srv.only("/loki/api/v1/query_range")
        assert '{service="order-service"}' in record.query["query"]
        assert '|= "timeout"' in record.query["query"]
        assert "timeout" in body["data"]["result"][0]["values"][0][1]


# --------------------------------------------------------------------------- #
# Tempo
# --------------------------------------------------------------------------- #


def test_tempo_service_traces_searches_then_fetches_each_trace_over_http() -> None:
    """先 /api/search 再对每个 traceID 拉 /api/traces/{id}，顺序要能被观察到。

    FakeTransport 只看得到"内存里的调用"，这里断言的是真实请求序列，
    能覆盖 search 响应的 traceID 是否被正确解析并用于后续路径拼接。
    """
    with serve(FakeTempoHandler) as srv:
        adapter = TempoAdapter(srv.base_url, transport=_transport())
        body = adapter.service_traces("order-service", START, END, min_duration_ms=500)

        paths = srv.paths()
        assert paths[0] == "/api/search"
        assert len(paths) == 3, f"expected 1 search + 2 trace fetches, got {paths}"
        trace_paths = paths[1:]
        assert all(p.startswith("/api/traces/") for p in trace_paths)
        assert len(set(trace_paths)) == 2  # 两条不同的 trace，不是同一个 id 拉两次

        search = srv.only("/api/search")
        assert search.query["tags"] == "service.name=order-service"
        assert search.query["minDuration"] == "500ms"
        # Tempo search 用 epoch 秒：纳秒会 >= 1e12 被假服务器 400 掉
        assert search.query["start"] == str(int(START_EPOCH))
        assert search.query["end"] == str(int(END_EPOCH))

        assert body["traces_considered"] == 2
        assert len(body["batches"]) == 2


# --------------------------------------------------------------------------- #
# 错误路径
# --------------------------------------------------------------------------- #


def test_prometheus_503_raises_transport_error_with_status() -> None:
    """503 走 urllib 的 HTTPError 分支，status 必须被保留下来。

    这是真实网络与假桩最大的差异点之一：假桩是把 (503, body) 当普通返回值，
    真链路是抛 HTTPError 后再被 `UrllibTransport` 转成 (status, body)。
    """
    with serve(FakePrometheusHandler, failure="http_503") as srv:
        adapter = PrometheusAdapter(srv.base_url, transport=_transport())
        with pytest.raises(TransportError) as excinfo:
            adapter.query_range("up", START, END)

        assert excinfo.value.status == 503
        assert "503" in str(excinfo.value)
        assert excinfo.value.body == {"status": "error", "error": "service unavailable"}
        assert srv.only("/api/v1/query_range").response_status == 503


def test_prometheus_error_status_body_raises_transport_error() -> None:
    """HTTP 200 + {"status": "error"} 也是失败（PromQL 语法错误就是这种返回）。

    只看 HTTP 状态码的适配器会把这种错误当成功，这是必须覆盖的分支。
    """
    with serve(FakePrometheusHandler, failure="error_status") as srv:
        adapter = PrometheusAdapter(srv.base_url, transport=_transport())
        with pytest.raises(TransportError) as excinfo:
            adapter.query_instant("bad{")

        assert "query parse error" in str(excinfo.value)
        assert excinfo.value.status is None  # 不是 HTTP 层错误
        assert excinfo.value.body["status"] == "error"
        assert srv.only("/api/v1/query").response_status == 200


def test_loki_500_raises_transport_error() -> None:
    """Loki 500 且 body 是纯文本：`_decode` 不能崩，错误信息要能带出来。"""
    with serve(FakeLokiHandler, failure="http_500") as srv:
        adapter = LokiAdapter(srv.base_url, transport=_transport())
        with pytest.raises(TransportError) as excinfo:
            adapter.service_logs("order-service", START, END)

        assert excinfo.value.status == 500
        assert "ingester unavailable" in str(excinfo.value.body)
        assert srv.only("/loki/api/v1/query_range").response_status == 500


def test_tempo_get_trace_404_raises_transport_error() -> None:
    """trace 不存在时 Tempo 返回 404，适配器必须抛 TransportError 而不是返回空 dict。

    用未知 traceID 触发，模拟真实"搜索到但 trace 已过期"的场景。
    """
    with serve(FakeTempoHandler) as srv:
        adapter = TempoAdapter(srv.base_url, transport=_transport())
        with pytest.raises(TransportError) as excinfo:
            adapter.get_trace("deadbeefdeadbeefdeadbeefdeadbeef")

        assert excinfo.value.status == 404
        assert "404" in str(excinfo.value)
        assert srv.only("/api/traces/deadbeefdeadbeefdeadbeefdeadbeef").response_status == 404


# --------------------------------------------------------------------------- #
# Grafana MCP 客户端（JSON-RPC over streamable HTTP）
# --------------------------------------------------------------------------- #


def test_mcp_call_tool_posts_jsonrpc_over_real_http() -> None:
    """call_tool 必须真的 POST 一段 JSON-RPC，且服务器能按字段解析。

    假服务器校验 `jsonrpc == "2.0"`、`method` 与 `params.name`，
    并把收到的 arguments 原样回显 —— 回显比对等于"参数确实到了服务端"。
    """
    arguments = {"expr": "up", "datasourceUid": "prom-1", "stepSeconds": 60}
    with serve(FakeGrafanaMCPHandler) as srv:
        client = GrafanaMCPClient(srv.base_url, transport=_transport())
        result = client.call_tool("query_prometheus", arguments)

        record = srv.only("/")
        assert record.method == "POST"
        assert record.body["jsonrpc"] == "2.0"
        assert record.body["method"] == "tools/call"
        assert record.body["params"]["name"] == "query_prometheus"
        assert record.body["params"]["arguments"] == arguments
        # 服务端把 arguments 原样塞进 text content，客户端要解析成 JSON 对象
        assert result["tool"] == "query_prometheus"
        assert result["echo"] == arguments


def test_mcp_call_tool_parses_sse_response() -> None:
    """SSE（text/event-stream）响应要能解析，这是 streamable HTTP 的另一种传输方式。

    这里走真实链路：`UrllibTransport._decode` 依据 Content-Type 把 body 交成文本，
    再由 `_parse_sse_or_json` 取 `data:` 行 —— 两段任何一处错都会失败。
    """
    with serve(FakeGrafanaMCPHandler, response_mode="sse") as srv:
        client = GrafanaMCPClient(srv.base_url, transport=_transport())
        result = client.call_tool("query_loki_logs", {"logql": '{service="order-service"}'})

        record = srv.only("/")
        assert record.response_content_type == "text/event-stream"
        assert result["tool"] == "query_loki_logs"
        assert result["echo"] == {"logql": '{service="order-service"}'}


def test_mcp_jsonrpc_error_object_raises_mcp_error() -> None:
    """JSON-RPC error 对象要变成 MCPError，不能当成成功结果继续解析。"""
    with serve(
        FakeGrafanaMCPHandler,
        rpc_error={"code": -32601, "message": "unknown tool 'nope'"},
    ) as srv:
        client = GrafanaMCPClient(srv.base_url, transport=_transport())
        with pytest.raises(MCPError) as excinfo:
            client.call_tool("nope", {})

        assert "-32601" in str(excinfo.value)
        assert "unknown tool" in str(excinfo.value)
        assert srv.only("/").response_status == 200


def test_mcp_tool_result_is_error_raises_mcp_error() -> None:
    """`result.isError = true` 是工具执行失败（JSON-RPC 层是成功的），必须抛 MCPError。"""
    with serve(FakeGrafanaMCPHandler, tool_error=True) as srv:
        client = GrafanaMCPClient(srv.base_url, transport=_transport())
        with pytest.raises(MCPError) as excinfo:
            client.call_tool("query_prometheus", {"expr": "up"})

        assert "connection refused" in str(excinfo.value)
        assert srv.only("/").response_status == 200


# --------------------------------------------------------------------------- #
# 端到端接线（DatasourceAdapters + ToolGateway）
# --------------------------------------------------------------------------- #


def test_gateway_error_rate_tool_end_to_end_over_http() -> None:
    """完整链路：Gateway → DatasourceAdapters → PrometheusAdapter → 真实 HTTP。

    这是"适配器真的能用"的最低证明：工具注册、参数 schema、权限、审计、
    表达式生成、HTTP 往返、响应解析全部串起来跑一遍。
    """
    with serve(FakePrometheusHandler) as srv:
        adapters = DatasourceAdapters(
            prometheus=PrometheusAdapter(srv.base_url, transport=_transport()),
            loki=LokiAdapter(srv.base_url, transport=_transport()),
            tempo=TempoAdapter(srv.base_url, transport=_transport()),
        )
        gw = ToolGateway()
        register_builtin_tools(gw, adapters)

        result = gw.invoke(
            "query_service_error_rate",
            {"service": "order-service", "start": START, "end": END},
            budget=BudgetManager(),
        )

        # 数据确实来自服务器：矩阵 + 由服务器按 5xx 表达式生成的比例值
        assert result["status"] == "success"
        assert result["data"]["resultType"] == "matrix"
        assert result["data"]["result"][0]["metric"]["service"] == "order-service"

        received = srv.only("/api/v1/query_range").query["query"]
        assert 'status=~"5.."' in received

        # 审计：成功路径必须记为 OK（失败路径会被记成 ERROR）
        assert [entry["outcome"] for entry in gw.audit_log()] == ["OK"]


# --------------------------------------------------------------------------- #
# 请求头
# --------------------------------------------------------------------------- #


def test_transport_sends_expected_headers() -> None:
    """真实请求必须带合理的请求头，否则会被前置代理/WAF 拒绝。

    GET 侧：`UrllibTransport` 自己不加头，只依赖 urllib 默认的 User-Agent；
    POST 侧（MCP streamable HTTP）：必须同时声明 Content-Type: application/json
    与 Accept 含 text/event-stream，否则真实 MCP server 无法选择传输方式
    （假服务器对缺失的 Accept 返回 406，因此能拿到结果即证明头是对的）。
    """
    with serve(FakePrometheusHandler) as prom_srv:
        PrometheusAdapter(prom_srv.base_url, transport=_transport()).query_range("up", START, END)
        get_record = prom_srv.only("/api/v1/query_range")
        user_agent = get_record.headers.get("User-Agent", "")
        assert user_agent, "GET 请求缺少 User-Agent，真实数据源的反爬/审计会拒绝"
        assert "python-urllib" in user_agent.lower()

    with serve(FakeGrafanaMCPHandler) as mcp_srv:
        GrafanaMCPClient(mcp_srv.base_url, transport=_transport()).call_tool("query_prometheus", {})
        post_record = mcp_srv.only("/")
        assert post_record.headers.get("User-Agent")
        accept = post_record.headers.get("Accept", "").lower()
        assert "application/json" in accept
        assert "text/event-stream" in accept
        assert "application/json" in post_record.headers.get("Content-Type", "").lower()
        # 请求体确实是 JSON（不是表单编码）
        assert isinstance(post_record.body, dict)


def _json_dumps(value: object) -> str:
    """供调试时打印响应体（保持与被测代码相同的序列化设定）。"""
    return json.dumps(value, ensure_ascii=False)
