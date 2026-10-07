"""出站 URL 守卫：http/https 之外的 scheme 一律拒绝。

## 为什么必须有这个

四个出站调用点此前都是裸 `urllib.request.urlopen`。实测
`urlopen("file:///etc/passwd")` 会**真的打开本地文件**（读到 9350 字节）：
数据源地址被误配成 `file://` 时，适配器会把任意本地文件当作"证据"
存进证据库 —— 证据引擎成了本地文件读取器。`urllib` 还原生支持
`ftp://` 与 `data:`，是同一个洞的另外两个出口。

## 为什么是 scheme 白名单，而不是完整的 SSRF 私网加固

OWASP 建议"拒绝环回/私网地址"，但 TraceOps 的数据源
（Prometheus/Loki/Tempo/Grafana）本来就部署在私网和 localhost 上，
那条规则会打断每一个真实部署。适用本项目的威胁模型是：

1. URL 由**操作者配置**，不是终端用户输入 —— 威胁是误配
   和未来新增面把不可信 URL 喂进来，不是直接的请求伪造；
2. 证据引擎会把取回的**任何内容**长期保存并进入 prompt 构建链，
   所以 `file://` 读到的不是"读取失败"，而是"泄进了证据库"。

## 为什么 UnsafeUrlError 子类化 ValueError

1. `self_tracing.export()` 的契约是**永不抛出**，它已经
   `except (URLError, OSError, ValueError)` —— 子类化让现有捕获自然覆盖；
2. `retry_http._DEFAULT_RETRYABLE_EXCEPTIONS` 刻意不包含 ValueError ——
   scheme 错误是确定性错误，立即失败，不浪费预算重试。

## 解析必须与 urlopen 一致

守卫用 `urllib.parse.urlsplit` 判定 scheme，urlopen 内部用的也是它，
因此大小写归一化（`FILE://` → `file`）、前导空白剥离等行为两边一致，
不存在"守卫看到一种解析、urlopen 看到另一种"的绕过窗口。
"""
from __future__ import annotations

from urllib.parse import urlsplit

#: 允许的出站 scheme。白名单而非黑名单：`urllib` 能处理的 scheme
#: 列表比想象的长（file/ftp/data/...），黑名单永远追不全。
ALLOWED_SCHEMES: tuple[str, ...] = ("http", "https")


class UnsafeUrlError(ValueError):
    """URL 的 scheme 不在白名单内，或缺少 host。"""


def assert_http_url(url: str) -> str:
    """校验出站 URL；不合法即抛 `UnsafeUrlError`。返回原 URL 便于链式使用。

    必须在**发起请求的边界**调用（紧邻 urlopen），而不是只在配置加载时：
    配置时校验是便利性，请求时校验才是不变式 —— 哪怕未来有新代码
    绕过配置层直接传 URL，边界仍然拦得住。
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"refusing to fetch non-http(s) URL {url!r}: "
            f"scheme {parts.scheme!r} is not in the allowlist {ALLOWED_SCHEMES}. "
            "Datasource, model and OTLP endpoints must be http or https; "
            "a file:// datasource would read local files into the evidence store."
        )
    if not parts.netloc:
        raise UnsafeUrlError(
            f"refusing to fetch URL {url!r} without a host")
    return url
