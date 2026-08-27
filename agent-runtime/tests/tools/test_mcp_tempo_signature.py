"""MCP Tempo 工具的参数名必须与真实服务一致。

## 为什么这条测试有价值

`MCPDatasourceAdapters` 最初按**文档**写的参数名（q / startRFC3339 / limit / traceId），
实测全部错误 —— mcp-grafana v2.1.1 的真实签名是
  search_tempo_traces: datasourceUid / query / start / end
  get_tempo_trace:     datasourceUid / trace_id / focus_span_id

服务端对未知参数会明确拒绝（`unknown arguments ...`），所以参数名错误 = 功能完全不可用。
这类"文档与实现不符"的问题只能靠对着真实服务跑一次才发现。

本测试把**实测到的签名固化下来**，防止有人按文档改回去。
"""
from __future__ import annotations

from traceops.tools.adapters import MCPDatasourceAdapters, GrafanaMCPClient

SEARCH_ALLOWED = {"datasourceUid", "query", "start", "end"}
GET_ALLOWED = {"datasourceUid", "trace_id", "focus_span_id"}


class RecordingClient:
    """记录调用参数，不真正发请求。"""

    def __init__(self, *, search_response=None, trace_response=None):
        self.calls: list[tuple[str, dict]] = []
        self._search = search_response or {"traces": []}
        self._trace = trace_response or {"batches": []}

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if name == "search_tempo_traces":
            return self._search
        if name == "get_tempo_trace":
            return self._trace
        raise AssertionError(f"unexpected tool {name}")


class TestSearchTempoTracesSignature:
    def test_uses_query_start_end_not_documented_names(self):
        client = RecordingClient()
        adapters = MCPDatasourceAdapters(client)

        adapters.query_service_traces("order-service", "2026-10-31T00:00:00Z",
                                      "2026-10-31T23:59:59Z")

        name, args = client.calls[0]
        assert name == "search_tempo_traces"
        assert set(args) == SEARCH_ALLOWED, (
            f"real server rejects unknown args; got {sorted(args)}")
        # 曾经错写成 q / startRFC3339 / limit —— 服务端会直接 400
        for wrong in ("q", "startRFC3339", "endRFC3339", "limit"):
            assert wrong not in args

    def test_traceql_uses_resource_service_name_syntax(self):
        """TraceQL 语法：`{ resource.service.name = "x" }` 有效，
        `{service.name="x"}` 会被 Tempo 判为 parse error。"""
        client = RecordingClient()
        MCPDatasourceAdapters(client).query_service_traces(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T23:59:59Z")
        query = client.calls[0][1]["query"]
        assert "resource.service.name" in query, query

    def test_datasource_uid_always_passed(self):
        """缺 datasourceUid 会被拒（"datasource with UID '' not found"）。"""
        client = RecordingClient()
        MCPDatasourceAdapters(client).query_service_traces(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T23:59:59Z")
        assert client.calls[0][1]["datasourceUid"] == "tempo"


class TestGetTempoTraceSignature:
    def test_uses_trace_id_not_traceId(self):
        client = RecordingClient(
            search_response={"traces": [{"traceID": "a" * 32}]},
            trace_response={"batches": [{"resource": {}, "spans": []}]})
        MCPDatasourceAdapters(client).query_service_traces(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T23:59:59Z")

        get_call = client.calls[1]
        assert get_call[0] == "get_tempo_trace"
        # 必须是允许集的**子集**：focus_span_id 是可选的，不必总是传
        assert set(get_call[1]) <= GET_ALLOWED, (
            f"real server rejects unknown args; got {sorted(get_call[1])}")
        assert "datasourceUid" in get_call[1] and "trace_id" in get_call[1]
        assert "traceId" not in get_call[1]

    def test_short_trace_id_is_padded_to_32(self):
        """真实 Tempo 的 search 会返回短 ID（如 29 位），存储的是 32 位。"""
        client = RecordingClient(
            search_response={"traces": [{"traceID": "102030405060708090a0b0c0d0e0f"}]},
            trace_response={"batches": []})
        MCPDatasourceAdapters(client).query_service_traces(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T23:59:59Z")

        passed_id = client.calls[1][1]["trace_id"]
        assert len(passed_id) == 32, f"got {len(passed_id)} chars: {passed_id}"
        assert passed_id.endswith("102030405060708090a0b0c0d0e0f")

    def test_no_get_call_when_search_empty(self):
        client = RecordingClient(search_response={"traces": []})
        MCPDatasourceAdapters(client).query_service_traces(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T23:59:59Z")
        assert len(client.calls) == 1, "no trace ids -> no get calls"
