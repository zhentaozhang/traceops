"""MCP 适配器必须返回与直连 HTTP 适配器**相同的原生形状**。

## 为什么这条测试价值极高

`MCPDatasourceAdapters` 声称自己是「生产推荐：不直连各数据源」的替代品，
但 Grafana MCP 的返回结构**不是** Prometheus / Loki / Tempo 的原生响应：

| 数据源 | MCP 实际返回 | Normalizer 要求的原生形状 |
|---|---|---|
| Prometheus | `{data: [{metric, values}], exploreUrl}` | `{resultType, result}` |
| Loki | `{data: [{timestamp, line, labels}], metadata}` | `{streams: [{stream, values}]}` |
| Tempo search | `{traces, metrics}` | `{batches: [...]}` |

上层 `_unwrap_raw` + `normalize` 只认原生形状。所以 MCP 路径的每一条证据
都会在 `normalize` 抛 ValueError，再被 `_collect_node` 当作 missing_data 吞掉——
**表面上调查在跑，实际一条证据都拿不到**，而且没有任何报错。

`test_mcp_tempo_signature.py` 只校验参数名，用 `RecordingClient` 手写了
`{"batches": []}` 这种「刚好是原生形状」的假响应，因此从未暴露这个问题。
本测试用**实测抄回来的真实响应**做 fixture，锁住「适配器负责转换形状」这条契约。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traceops.domain.models import EvidenceType
from traceops.evidence.normalizer import normalize
from traceops.orchestration.graph import _unwrap_raw
from traceops.tools.adapters import MCPDatasourceAdapters


# --------------------------------------------------------------------------
# 实测抄回的真实响应（2026-10-31，mcp-grafana 运行在 compose 里，
# 对真实 Prometheus/Loki/Tempo 发起查询）
# --------------------------------------------------------------------------

REAL_MCP_PROMETHEUS = {
    "data": [{"metric": {}, "values": [[1793408400, "1"], [1793408460, "1"]]}],
    "exploreUrl": "http://localhost:3000/explore",
}

REAL_MCP_LOKI = {
    "data": [
        {
            "timestamp": "1793417622798029056",
            "line": "ERROR redis timeout trace_id=aa11bb22cc33dd44",
            "labels": {"level": "error", "service": "order-service"},
            "structuredMetadata": {"detected_level": "error"},
        }
    ],
    "metadata": {"linesReturned": 1, "totalLinesScanned": 1},
}


class StubClient:
    """按工具名返回预先抄好的真实响应，并记录调用参数。"""

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        if name not in self._responses:
            raise AssertionError(f"unexpected tool {name}")
        return self._responses[name]


def _normalize(evidence_type: EvidenceType, raw: object) -> object:
    """走与 `_collect_node` 完全相同的路径：unwrap → normalize。"""
    now = datetime.now(UTC)
    unwrapped = _unwrap_raw(raw, evidence_type)
    return normalize(
        evidence_type=evidence_type,
        source_id="grafana-mcp",
        incident_id="inc-1",
        run_id="run-1",
        service_id="order-service",
        query_template_id="tpl-1",
        query_params={},
        raw_payload=unwrapped,
        observed_at=now,
        window=(now, now),
    )


class TestPrometheusParams:
    """query_prometheus 的真实签名（实测，非文档）。

    valid arguments: datasourceUid, endTime, expr, projectName, queryType, startTime, stepSeconds
    必填：datasourceUid, expr, endTime
    """

    def test_sends_startTime_endTime_not_rfc3339_variants(self):
        client = StubClient({"query_prometheus": REAL_MCP_PROMETHEUS})
        MCPDatasourceAdapters(client).query_service_error_rate(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T01:00:00Z")

        assert client.calls[0][0] == "query_prometheus"
        args = client.calls[0][1]
        # 实测服务端对这两个名字直接拒绝：
        #   unknown arguments "endRFC3339", "startRFC3339"
        assert "startTime" in args, args
        assert "endTime" in args, args
        assert "startRFC3339" not in args
        assert "endRFC3339" not in args

    def test_range_query_declares_query_type(self):
        """不给 queryType 时，带 stepSeconds 的 range 查询无法成立。"""
        client = StubClient({"query_prometheus": REAL_MCP_PROMETHEUS})
        MCPDatasourceAdapters(client).query_service_error_rate(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T01:00:00Z")
        assert client.calls[0][1].get("queryType") == "range"


class TestPrometheusShape:
    def test_metric_response_becomes_prometheus_native_shape(self):
        """MCP 的 {data: [...]} 必须转成 {resultType, result} 才能进 Normalizer。"""
        client = StubClient({"query_prometheus": REAL_MCP_PROMETHEUS})
        raw = MCPDatasourceAdapters(client).query_service_error_rate(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T01:00:00Z")

        evidence = _normalize(EvidenceType.METRIC, raw)
        assert evidence.normalized_data["result_type"] == "matrix"
        assert evidence.normalized_data["result_count"] == 1


class TestLokiParams:
    """query_loki_logs 的真实签名。

    valid arguments: datasourceUid, direction, endRfc3339, format, limit,
                     logql, queryType, startRfc3339, stepSeconds
    """

    def test_sends_exact_rfc3339_field_names(self):
        client = StubClient({"query_loki_logs": REAL_MCP_LOKI})
        MCPDatasourceAdapters(client).query_service_logs(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T01:00:00Z")

        args = client.calls[0][1]
        # schema 里的规范拼写是小写 fc；Go 的 JSON 解码容忍大小写差异，
        # 但写错名字在别的实现上就会坏，所以按 schema 原样写。
        assert "startRfc3339" in args, args
        assert "endRfc3339" in args, args


class TestLokiShape:
    def test_log_response_becomes_loki_native_shape(self):
        """MCP 逐条 {timestamp, line, labels} 必须聚成 {streams: [{stream, values}]}。"""
        client = StubClient({"query_loki_logs": REAL_MCP_LOKI})
        raw = MCPDatasourceAdapters(client).query_service_logs(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T01:00:00Z")

        evidence = _normalize(EvidenceType.LOG, raw)
        assert evidence.normalized_data["total_records"] == 1
        assert evidence.normalized_data["records"][0]["message"].startswith("ERROR redis timeout")


class TestTempoSearchShape:
    def test_search_only_response_is_empty_evidence_not_crash(self):
        """search 返回 {traces, metrics}，没有任何 span —— 不能伪装成证据。"""
        client = StubClient({
            "search_tempo_traces": {"traces": [], "metrics": {}},
            "get_tempo_trace": {"batches": []},
        })
        raw = MCPDatasourceAdapters(client).query_service_traces(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T01:00:00Z")

        # 空 trace 列表 → 无 batch。这是"没有数据"，`_collect_node` 应记 missing_data。
        assert raw["batches"] == []
        with pytest.raises(ValueError, match="no spans"):
            _normalize(EvidenceType.TRACE, raw)


class TestPrometheusTimeFormat:
    """Prometheus 的时间解析器（gtime）**不接受 6 位微秒**。

    实测：
      2026-10-31T03:36:00Z                    OK
      2026-10-31T03:36:00.123Z                OK
      2026-10-31T03:36:00+00:00               OK
      2026-10-31T03:36:00.123456+00:00        FAIL
        -> parsing end time: syntax error: unexpected tDIGIT ... starting with "0"

    而 `datetime.isoformat()` 默认就输出 6 位微秒，`_plan_node` 传入的正是
    isoformat 结果。所以"参数名改对了"还不够——时间格式也必须规整，
    否则真实调用仍然 100% 失败（只是错误信息变成了 parse error）。
    """

    def test_microsecond_isoformat_is_accepted(self):
        client = StubClient({"query_prometheus": REAL_MCP_PROMETHEUS})
        start = datetime(2026, 10, 31, 3, 36, 0, 123456, tzinfo=UTC)
        end = datetime(2026, 10, 31, 3, 37, 0, 654321, tzinfo=UTC)

        MCPDatasourceAdapters(client).query_service_error_rate(
            "order-service", start.isoformat(), end.isoformat())

        args = client.calls[0][1]
        # 6 位小数必须被裁到毫秒精度（或去掉），否则服务端拒收
        for field in ("startTime", "endTime"):
            value = args[field]
            frac = value.split(".")[1].split("+")[0].split("Z")[0] if "." in value else ""
            assert len(frac) <= 3, f"{field}={value!r} has {len(frac)} fractional digits"

    def test_explicit_z_timestamp_passes_through_unchanged(self):
        """已经是服务端接受的格式时不要改写（避免把 'now-1h' 这类相对时间搞坏）。"""
        client = StubClient({"query_prometheus": REAL_MCP_PROMETHEUS})
        MCPDatasourceAdapters(client).query_service_error_rate(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T01:00:00Z")
        assert client.calls[0][1]["startTime"] == "2026-10-31T00:00:00Z"

    def test_relative_time_is_left_alone(self):
        """`now-15m` 是服务端支持的相对格式，不能被转成绝对时间戳。"""
        client = StubClient({"query_prometheus": REAL_MCP_PROMETHEUS})
        MCPDatasourceAdapters(client).query_metric_series(
            "order-service", "up", "now-15m", "now")
        assert client.calls[0][1]["startTime"] == "now-15m"
        assert client.calls[0][1]["endTime"] == "now"


# 实测抄回：search_tempo_traces 的真实响应（含 spanSet）。
# 关键点：search 就已经带回了 span 列表，即使 get_tempo_trace 不可用，
# 也足以构造 Trace 证据——只是少了完整 span 树。
REAL_MCP_TEMPO_SEARCH = {
    "traces": [
        {
            "traceID": "aa11bb22cc33dd44ee55ff6677889900",
            "rootServiceName": "redis",
            "rootTraceName": "redis.command",
            "startTimeUnixNano": "1793417342342042880",
            "durationMs": 2100,
            "spanSet": {
                "spans": [
                    {
                        "spanID": "00f067aa0ba902b7",
                        "startTimeUnixNano": "1793417342342042880",
                        "durationNanos": "2100001024",
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "redis"}}
                        ],
                    }
                ],
                "matched": 1,
            },
        }
    ],
    "metrics": {},
}


class TestTempoDegradesWhenTraceFetchUnavailable:
    """get_tempo_trace 在某些 mcp-grafana / Tempo 版本组合下恒返回 404。

    实测（Grafana 11.1.0 + Tempo 2.5.0 + mcp-grafana 2.1.1 容器）：
        GET /api/datasources/proxy/uid/tempo/api/v2/traces/{id} -> 404
    而同一 trace 直连 Tempo 的 `/api/traces/{id}` 返回 200。

    这意味着整条 trace 证据路径在该组合下完全失效。但 `search_tempo_traces`
    本身是好的，且**它已经返回了 spanSet**（spanID / durationNanos /
    属性齐全）。因此正确做法不是让整个调查降级为"无 trace 数据"，而是
    用 search 的 spanSet 构造证据，并在日志/报告中标注这是降级结果。
    """

    def test_falls_back_to_search_span_set_when_get_trace_fails(self):
        class FailingGetClient:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments=None):
                self.calls.append((name, dict(arguments or {})))
                if name == "search_tempo_traces":
                    return REAL_MCP_TEMPO_SEARCH
                raise RuntimeError("tempo API returned 404: 404 page not found")

        client = FailingGetClient()
        raw = MCPDatasourceAdapters(client).query_service_traces(
            "redis", "now-2h", "now")

        # 不能抛异常；必须仍然产出可归一化的证据
        assert raw["traces_considered"] == 1
        assert raw["batches"], "search spanSet should be used as a fallback"
        evidence = _normalize(EvidenceType.TRACE, raw)
        assert evidence.normalized_data["total_records"] >= 1

    def test_fallback_is_marked_so_it_is_not_mistaken_for_full_trace(self):
        """降级结果必须可辨：否则"只有摘要 span"会被误当成完整 span 树。"""
        class FailingGetClient:
            def call_tool(self, name, arguments=None):
                if name == "search_tempo_traces":
                    return REAL_MCP_TEMPO_SEARCH
                raise RuntimeError("tempo API returned 404: 404 page not found")

        raw = MCPDatasourceAdapters(FailingGetClient()).query_service_traces(
            "redis", "now-2h", "now")
        assert raw.get("degraded") is True
        assert "get_tempo_trace" in (raw.get("degraded_reason") or "")

    def test_degraded_flag_survives_into_evidence(self):
        """降级标记必须一路带到 Evidence，否则下游无法判断数据完整性。

        降级证据只有"匹配到的 span"，没有父子结构。如果这个标记在
        `normalize` 时被丢掉，报告会把摘要当完整 span 树引用——
        这正是"证据可信"要防的事（引用完整性可以过，语义却错）。
        """
        class FailingGetClient:
            def call_tool(self, name, arguments=None):
                if name == "search_tempo_traces":
                    return REAL_MCP_TEMPO_SEARCH
                raise RuntimeError("tempo API returned 404: 404 page not found")

        raw = MCPDatasourceAdapters(FailingGetClient()).query_service_traces(
            "redis", "now-2h", "now")
        evidence = _normalize(EvidenceType.TRACE, raw)
        assert evidence.normalized_data.get("degraded") is True
        assert evidence.normalized_data.get("degraded_reason")

    def test_successful_get_does_not_claim_degraded(self):
        client = StubClient({
            "search_tempo_traces": REAL_MCP_TEMPO_SEARCH,
            "get_tempo_trace": {
                "batches": [{
                    "resource": {"attributes": [
                        {"key": "service.name", "value": {"stringValue": "redis"}}]},
                    "scopeSpans": [{"spans": [{
                        "traceId": "aa11bb22cc33dd44ee55ff6677889900",
                        "spanId": "00f067aa0ba902b7",
                        "name": "redis.command",
                        "startTimeUnixNano": "1793417342342042880",
                        "endTimeUnixNano": "1793417344442042880",
                        "status": {"code": "STATUS_CODE_ERROR"},
                    }]}],
                }]
            },
        })
        raw = MCPDatasourceAdapters(client).query_service_traces(
            "redis", "now-2h", "now")
        assert raw.get("degraded") is False


class TestTempoV3Shape:
    """Tempo v3 的 `/api/v2/traces/{id}` 把 span 树包在 `trace` 字段里。

    实测（Tempo v3.0.0）：
        {"trace": {"resourceSpans": [{resource, scopeSpans}]}, "metrics": {}}
    而 Tempo 2.5 的老端点是直接 `{"batches": [...]}`。

    适配器当前写的是 `payload.get("batches", [payload])` —— 对 v3 来说
    `batches` 不存在，于是把**整个外层 dict** 当成一个 batch 塞进去。
    那个 dict 里没有 `spans`/`scopeSpans`，`TraceNormalizer` 判定
    "contains no spans"，证据照样丢失。

    也就是说：升 Tempo 版本并不会自动变好，这里还有一层形状要对上。
    """

    def test_v3_trace_wrapper_is_unwrapped_into_batches(self):
        class V3Client:
            def call_tool(self, name, arguments=None):
                if name == "search_tempo_traces":
                    return {"traces": [{"traceID": "a" * 32}], "metrics": {}}
                return {
                    "trace": {
                        "resourceSpans": [{
                            "resource": {"attributes": [
                                {"key": "service.name", "value": {"stringValue": "redis"}}]},
                            "scopeSpans": [{"spans": [{
                                "traceId": "a" * 32,
                                "spanId": "22bb33cc44dd55ee",
                                "name": "redis.command",
                                "startTimeUnixNano": "1793417000000000000",
                                "endTimeUnixNano": "1793417002100000000",
                                "status": {"code": "STATUS_CODE_ERROR"},
                            }]}],
                        }]
                    },
                    "metrics": {},
                }

        raw = MCPDatasourceAdapters(V3Client()).query_service_traces(
            "redis", "now-1h", "now")

        evidence = _normalize(EvidenceType.TRACE, raw)
        assert evidence.normalized_data["total_records"] == 1
        span = evidence.normalized_data["spans"][0]
        assert span["service"] == "redis"
        assert span["operation_name"] == "redis.command"
        assert span["error"] is True

    def test_v3_result_is_not_marked_degraded(self):
        """v3 给的是完整 span 树，不能标成降级。"""

        class V3Client:
            def call_tool(self, name, arguments=None):
                if name == "search_tempo_traces":
                    return {"traces": [{"traceID": "a" * 32}], "metrics": {}}
                return {"trace": {"resourceSpans": [{
                    "resource": {"attributes": [
                        {"key": "service.name", "value": {"stringValue": "redis"}}]},
                    "scopeSpans": [{"spans": [{
                        "traceId": "a" * 32, "spanId": "b", "name": "redis.command",
                        "startTimeUnixNano": "1", "endTimeUnixNano": "2"}]}],
                }]}, "metrics": {}}

        raw = MCPDatasourceAdapters(V3Client()).query_service_traces(
            "redis", "now-1h", "now")
        assert raw.get("degraded") is False


class TestAdapterContractParity:
    """MCP 适配器与直连适配器对同一份数据必须产出可比的 Evidence。"""

    def test_all_three_types_normalize_without_error(self):
        client = StubClient({
            "query_prometheus": REAL_MCP_PROMETHEUS,
            "query_loki_logs": REAL_MCP_LOKI,
            "search_tempo_traces": {"traces": [], "metrics": {}},
        })
        adapters = MCPDatasourceAdapters(client)

        metric_raw = adapters.query_service_error_rate(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T01:00:00Z")
        log_raw = adapters.query_service_logs(
            "order-service", "2026-10-31T00:00:00Z", "2026-10-31T01:00:00Z")

        # 两类都必须在**不抛异常**的前提下产出证据
        assert _normalize(EvidenceType.METRIC, metric_raw) is not None
        assert _normalize(EvidenceType.LOG, log_raw) is not None


class TestDsQueryFallbackPrecedence:
    """降级顺序必须先优后劣：ds/query 完整 span 树 > search 摘要 spanSet。

    实测：Tempo 2.5 下 get_tempo_trace 恒 404，但 Grafana /api/ds/query
    的 traceId 查询返回 200 且带 parentSpanID —— 即可重建 span 树。
    所以挂了 ds/query 就不该退到"只剩摘要"的 degraded 路径。
    """

    def test_full_tree_is_preferred_over_degraded_summary(self):
        class _Fallback:
            def __init__(self):
                self.calls = []

            def get_trace_batches(self, trace_id):
                self.calls.append(trace_id)
                return [{
                    "resource": {"attributes": [
                        {"key": "service.name", "value": {"stringValue": "redis"}}]},
                    "scopeSpans": [{"spans": [{
                        "traceId": trace_id, "spanId": "b", "name": "redis.command",
                        "startTimeUnixNano": "1793417000000000000",
                        "endTimeUnixNano": "1793417002100000000",
                        "status": {"code": "STATUS_CODE_ERROR"}}]}],
                }]

        class _Client:
            def call_tool(self, name, arguments=None):
                if name == "search_tempo_traces":
                    return REAL_MCP_TEMPO_SEARCH
                raise RuntimeError("tempo API returned 404: 404 page not found")

        fallback = _Fallback()
        adapters = MCPDatasourceAdapters(_Client()).with_trace_fallback(fallback)
        raw = adapters.query_service_traces("redis", "now-2h", "now")

        assert fallback.calls, "ds/query fallback must be attempted"
        # 拿到完整树 → 不应标成 degraded
        assert raw.get("degraded") is False
        evidence = _normalize(EvidenceType.TRACE, raw)
        assert evidence.normalized_data["total_records"] == 1
        assert "degraded" not in evidence.normalized_data

    def test_falls_to_summary_when_dsquery_also_fails(self):
        class _Fallback:
            def get_trace_batches(self, trace_id):
                raise RuntimeError("ds/query unavailable")

        class _Client:
            def call_tool(self, name, arguments=None):
                if name == "search_tempo_traces":
                    return REAL_MCP_TEMPO_SEARCH
                raise RuntimeError("tempo API returned 404: 404 page not found")

        raw = MCPDatasourceAdapters(_Client()).with_trace_fallback(_Fallback()) \
            .query_service_traces("redis", "now-2h", "now")
        assert raw.get("degraded") is True, "summary fallback must be marked degraded"
