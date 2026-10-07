"""出站 URL 必须在 scheme 白名单内：http/https 之外的 scheme 一律拒绝。

## 缺陷（实测确认，不是理论推演）

四个出站调用点全部是裸 `urllib.request.urlopen`，没有任何 scheme 校验。
实测 `urlopen("file:///etc/passwd")` **真的打开了本地文件**（读到 9350 字节）。
这意味着：数据源地址被误配成（或被攻击者改写成）`file://` 时，
适配器会把任意本地文件当作"证据"存进证据库 —— 证据引擎成了本地文件读取器。

`urllib` 还支持 `ftp://` 和 `data:` —— 同一个洞的另外两个出口。

## 威胁模型（为什么是 scheme 白名单，而不是完整的 SSRF 加固）

OWASP SSRF 防护建议"拒绝环回/私网地址"——但 TraceOps 的数据源
（Prometheus/Loki/Tempo/Grafana）**本来就部署在私网和 localhost 上**，
那条规则会打断每一个真实部署，不能照搬。真正适用的部分是：

1. URL 由**操作者配置**，不是终端用户输入 —— 威胁是"误配"和
   "未来某个新增面把不可信 URL 喂进来"，不是直接的请求伪造；
2. 证据引擎会把取回的**任何内容**作为证据长期保存并进入 prompt 构建链，
   所以 `file://` 读到的本地文件不是"读取失败"，而是"泄进了证据库"。

因此本守卫做 scheme 白名单（http/https），并刻意不做私网地址排除。

## 为什么 UnsafeUrlError 子类化 ValueError

1. `self_tracing.export()` 的契约是**永不抛出**（可观测性不能成为故障源），
   它已经 `except (URLError, OSError, ValueError)` —— 子类化让现有捕获自然覆盖；
2. `retry_http._DEFAULT_RETRYABLE_EXCEPTIONS` 刻意**不包含** ValueError —— 
   scheme 错误是确定性错误，立即失败，不浪费预算重试。

## 四个出站点（全部要接上守卫）

- `tools/adapters.py` `UrllibTransport` —— 所有 HTTP 数据源适配器 + MCP 客户端的唯一咽喉
- `tools/tempo_dsquery.py` `GrafanaDsQueryClient` —— Grafana /api/ds/query 兜底路径
- `runtime/self_tracing.py` `TraceRecorder.export` —— OTLP 上报
- `runtime/openai_client.py` `_UrllibTransport.post` —— 模型调用
"""
from __future__ import annotations

import pytest

from traceops.security.urls import UnsafeUrlError, assert_http_url


class TestSchemeAllowlist:
    def test_http_and_https_pass(self):
        assert_http_url("http://localhost:9090/api/v1/query")
        assert_http_url("https://grafana.example.com/api/ds/query")

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",          # 实测确认能打开本地文件
        "ftp://evil.example.com/x",    # urllib 原生支持
        "data:text/plain,hello",       # urllib 原生支持
        "gopher://evil.example.com",   # 历史上 SSRF 的经典出口
        "/etc/passwd",                 # 无 scheme
        "//evil.example.com/x",        # 协议相对 URL，无 scheme
        "",                            # 空
    ])
    def test_everything_else_is_refused(self, url):
        with pytest.raises(UnsafeUrlError):
            assert_http_url(url)

    def test_uppercase_scheme_is_not_a_bypass(self):
        # urlsplit 会把 scheme 小写化 —— 守卫依赖的解析必须和 urlopen 一致
        with pytest.raises(UnsafeUrlError):
            assert_http_url("FILE:///etc/passwd")

    def test_leading_whitespace_is_not_a_bypass(self):
        # 同上：urlsplit 对前导空白做了归一化
        with pytest.raises(UnsafeUrlError):
            assert_http_url(" file:///etc/passwd")

    def test_error_names_the_rejected_scheme(self):
        with pytest.raises(UnsafeUrlError, match="file"):
            assert_http_url("file:///etc/passwd")

    def test_error_is_a_value_error(self):
        """self_tracing 的现有 except 与 retry 的不可重试集合都依赖这一点。"""
        assert issubclass(UnsafeUrlError, ValueError)

    def test_host_is_required(self):
        # 无 host 的 http URL 到 urlopen 也会失败，但错误会很费解；
        # 守卫给出明确报因
        with pytest.raises(UnsafeUrlError, match="host"):
            assert_http_url("http:///path")


class TestTransportGuardIsWired:
    """守卫必须接在每个出站点上 —— 没接的守卫等于不存在。"""

    def test_urllib_transport_refuses_file_scheme(self):
        """实测过的洞：修复前这一调用会**真的读到 /etc/passwd**。"""
        from traceops.tools.adapters import UrllibTransport

        with pytest.raises(UnsafeUrlError):
            UrllibTransport().get("file:///etc/passwd")

    def test_urllib_transport_post_refuses_file_scheme(self):
        from traceops.tools.adapters import UrllibTransport

        with pytest.raises(UnsafeUrlError):
            UrllibTransport().post("file:///tmp/x", {"q": 1})

    def test_urllib_transport_allows_https_through_to_the_network(self):
        """https 放行：守卫之后到达网络层（这里没有服务，报连接类错误，
        而不是 UnsafeUrlError —— 证明拒绝不是无差别的）。"""
        from traceops.tools.adapters import TransportError, UrllibTransport

        with pytest.raises(TransportError) as excinfo:
            UrllibTransport().get("https://127.0.0.1:1/q", timeout=0.2)
        assert not isinstance(excinfo.value, UnsafeUrlError)

    def test_dsquery_client_refuses_file_scheme(self):
        from traceops.tools.tempo_dsquery import GrafanaDsQueryClient

        client = GrafanaDsQueryClient(base_url="file:///etc", datasource_uid="tempo")
        with pytest.raises(UnsafeUrlError):
            client.get_trace_batches("abc123")

    def test_self_tracing_export_never_throws_on_bad_endpoint(self):
        """export() 的契约是**永不抛出**：坏端点返回 False，不是异常。"""
        from traceops.runtime.self_tracing import TraceRecorder

        recorder = TraceRecorder(service_name="traceops-agent",
                                 endpoint="file:///tmp/never", enabled=True)
        with recorder.span("collect"):
            pass
        assert recorder.export() is False

    def test_model_transport_refuses_file_scheme(self):
        from traceops.runtime.openai_client import _UrllibTransport

        with pytest.raises(UnsafeUrlError):
            _UrllibTransport().post("file:///etc/passwd", {"model": "x"})


class TestGuardCoversTheAdapterLayer:
    """适配器（Prometheus/Loki/Tempo HTTP 路径）全部经由 UrllibTransport ——
    守卫在咽喉处，因此一个错误配置的适配器基地址同样被拒。"""

    def test_prometheus_adapter_with_file_base_url_is_refused(self):
        from traceops.tools.adapters import PrometheusAdapter

        adapter = PrometheusAdapter("file:///var/lib/prometheus")
        with pytest.raises(UnsafeUrlError):
            adapter.query_range("up", start="2026-10-30T10:00:00Z",
                                end="2026-10-30T10:01:00Z")
