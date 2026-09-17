"""自观测：TraceOps 自身产生 OTLP trace。

## 为什么这不是锦上添花（PRD 里「TraceOps 自身接 OTel」长期是 ⬜）

一个诊断平台如果自己是黑盒，会有很难自查的盲区：

- 调查为什么慢？没有 span，只能靠日志猜
- 哪个节点耗时最多？只能看到总时长
- 工具调用延迟分布？无数据

更关键的是**双向验证**：TraceOps 已经在消费别人的 OTLP trace
（`evidence/normalizer.py` 解析 `batches[].scopeSpans[].spans[]`）。
如果它自己产生的 trace 不能被同一条链路接受并查回，说明它对 OTLP 的
理解有缺口 —— 那个缺口同样会污染它对别人数据的解析。

## 约束：绝不引入 OTel SDK

`pyproject.toml` 刻意只依赖标准库 + pydantic + jsonschema。
OTLP/HTTP-JSON 就是一个普通 JSON POST，标准库足够 ——
项目已经这样往 Tempo push 过 trace（`scripts/e2e_live_stack.py`）。

## 实测教训：id 必须是十六进制

OTLP 规范说 trace/span id 是 base64，但**实测** Tempo 2.5 的 OTLP 端点
拒绝 base64（报 `invalid length for ID`），只接受 hex。
这条已经在生产代码里踩过（`normalizer.py` 分组前先判 hex 形状），
自观测不能重蹈覆辙 —— 因此这里直接生成 hex。

## 三条硬约束

1. **上报失败不影响业务**：`export()` 返回 False，绝不抛异常
2. **默认关闭**：自观测是诊断辅助，不该成为默认开销
3. **嵌套要正确**：collect 内部的工具调用是它的子 span，否则耗时无法归因
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator


class SpanStatus(str, Enum):
    OK = "STATUS_CODE_OK"
    ERROR = "STATUS_CODE_ERROR"
    UNSET = "STATUS_CODE_UNSET"


def _hex_id(length: int) -> str:
    """生成指定长度的十六进制 id（32 = traceId，16 = spanId）。

    不用 base64：Tempo 的 OTLP 端点实测拒绝 base64。
    """
    return uuid.uuid4().hex[:length]


@dataclass
class Span:
    """一个 OTLP span（JSON 表示）。"""

    name: str
    start_unix_nano: int
    end_unix_nano: int
    status: SpanStatus = SpanStatus.UNSET
    trace_id: str = field(default_factory=lambda: _hex_id(32))
    span_id: str = field(default_factory=lambda: _hex_id(16))
    parent_span_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_otlp(self) -> dict[str, Any]:
        attrs = [
            {"key": key, "value": {"stringValue": str(value)}}
            for key, value in self.attributes.items()
        ]
        span: dict[str, Any] = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "startTimeUnixNano": str(self.start_unix_nano),
            "endTimeUnixNano": str(self.end_unix_nano),
            "status": {"code": self.status.value},
        }
        if self.parent_span_id:
            span["parentSpanId"] = self.parent_span_id
        if attrs:
            span["attributes"] = attrs
        return span


class TraceRecorder:
    """收集 span 并能导出成 OTLP/HTTP-JSON。"""

    def __init__(self, *, service_name: str, endpoint: str | None = None,
                 enabled: bool = True, timeout: float = 5.0) -> None:
        self.service_name = service_name
        self.endpoint = endpoint or os.environ.get("TRACEOPS_OTLP_ENDPOINT")
        self.enabled = enabled
        self.timeout = timeout
        self._spans: list[Span] = []
        # 共享 trace id：一次调查的所有 span 属于同一条 trace，
        # 否则在 Tempo 里会散成 N 条互不关联的单 span trace。
        self._trace_id = _hex_id(32)
        self._stack: list[str] = []

    def spans(self) -> list[Span]:
        return list(self._spans)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span | None]:
        """记录一段代码的耗时。

        作为上下文管理器使用：正常退出记 OK，异常退出记 ERROR 并把异常继续抛。
        **观测不改变控制流** —— 异常照常传播。
        """
        if not self.enabled:
            yield None
            return

        parent = self._stack[-1] if self._stack else ""
        span = Span(
            name=name,
            start_unix_nano=time.time_ns(),
            end_unix_nano=time.time_ns(),
            status=SpanStatus.UNSET,
            trace_id=self._trace_id,
            parent_span_id=parent,
            attributes=dict(attributes),
        )
        self._stack.append(span.span_id)
        try:
            yield span
            span.status = SpanStatus.OK
        except Exception:
            span.status = SpanStatus.ERROR
            raise
        finally:
            # finally 保证异常路径也记录耗时 —— 失败调用的耗时同样有价值
            span.end_unix_nano = time.time_ns()
            self._stack.pop()
            self._spans.append(span)

    def to_otlp_payload(self) -> dict[str, Any]:
        """组建成 OTLP/HTTP-JSON 的 `resourceSpans` 结构。"""
        spans = [span.to_otlp() for span in self._spans]
        return {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name",
                         "value": {"stringValue": self.service_name}},
                    ]
                },
                "scopeSpans": [{"scope": {"name": "traceops.self"}, "spans": spans}],
            }]
        }

    def export(self) -> bool:
        """POST 到 OTLP 端点。**失败返回 False，绝不抛异常**。

        理由：观测是辅助能力。如果上报失败能让调查失败，那观测就成了
        新的故障源 —— 宁可不观测，也不能让排查工具因为自己而崩。
        """
        if not self.enabled:
            return True
        if not self.endpoint or not self._spans:
            return True
        payload = json.dumps(self.to_otlp_payload(), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def clear(self) -> None:
        """清空已收集的 span（导出后调用，避免同一批重复上报）。"""
        self._spans.clear()
