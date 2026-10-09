"""agent-runtime 的 Redis 发布端。

此前**没有写端**：worker 能从 Redis 读 `investigation.requested`，
但产出的 `investigation.completed` / `investigation.failed` 只进内存列表
然后打到 stdout。真实链路上是这样断的::

    Java ──XADD──▶ Redis ──▶ Python worker ──(只打印)──▶ ✗ 控制面收不到终态

于是控制面永远看不到结论：既拿不到诊断，也拿不到 `retryable`，
"失败 → 安排重试"这一环在真实传输上从未发生过。

这一模块补上写端，与 Java 侧 `RedisStreamEventPublisher` 对称：

* 同一个流（默认 ``traceops.events``）
* 同一个字段名（``payload``，里面装**整份** ``{"envelope": ..., "payload": ...}``）
* 同一条规则：**XADD 没有回执就算失败**，不静默吞掉

字段名与嵌套形状必须两侧一致，这不是风格问题：两侧 reader 各自按字段名取值，
名字一旦不一致，Python 写的事件在 Java 侧会读成空 payload，
然后被当成"无法使用的消息"丢进毒消息分支 —— 静默失败，最难查的那种。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

__all__ = [
    "RedisStreamEventSink",
    "SinkPublishError",
    "RedisPublishTransport",
]


class SinkPublishError(RuntimeError):
    """事件没能进入 Redis 流。

    单独一个异常类型（而不是复用 RedisConnectionError）是有意的：
    上层要区分"网络/连接坏了"和"消息本身发不出去"——
    前者可以重连再试，后者重试也是同样的结果。
    """


class RedisPublishTransport(Protocol):
    """写端需要的最小传输层。

    只要一个 ``xadd``：把一段 JSON 文本塞进流的某个字段。
    为什么不用 redis-py：仓库里的读端是自写 RESP2 客户端，
    写端再引入一个第三方依赖会让"同一条连接、同一种编码"这个前提不再成立。
    """

    def xadd(self, stream: str, payload: str, *, max_len: int | None = None) -> Any:
        """`XADD <stream> [MAXLEN ~ <n>] * payload <json>`，返回条目 id。

        返回 ``None`` 表示**没有拿到回执**，即事件没有发出去。
        """
        ...


class RedisStreamEventSink:
    """满足 worker.py `EventSink` Protocol 的 Redis Streams 实现。

    只做一件事：把一份事件文档写进流里。写入失败一律抛
    ``SinkPublishError``，绝不"吞掉并假装成功"。
    """

    #: 与 Java 侧 ``RedisStreamEventPublisher.FIELD`` 一致。
    FIELD = "payload"

    #: 与 Java 侧 ``RedisStreamEventPublisher.STREAM_KEY`` 一致。
    DEFAULT_STREAM = "traceops.events"

    #: 与 Java 侧 ``RedisStreamEventPublisher.DEFAULT_MAX_LEN`` 一致。
    DEFAULT_MAX_LEN = 10000

    def __init__(self, transport: RedisPublishTransport, *, stream: str = DEFAULT_STREAM,
                 max_len: int | None = DEFAULT_MAX_LEN) -> None:
        if not isinstance(stream, str) or not stream.strip():
            raise ValueError(f"stream must be a non-empty str, got {stream!r}")
        if max_len is not None:
            if isinstance(max_len, bool) or not isinstance(max_len, int):
                raise ValueError(f"max_len must be an int or None, got {type(max_len).__name__}")
            if max_len <= 0:
                raise ValueError(f"max_len must be > 0, got {max_len!r}")
        self._transport = transport
        self._stream = stream
        self._max_len = max_len

    @property
    def stream(self) -> str:
        return self._stream

    def publish(self, message: dict[str, Any]) -> None:
        """把一份事件文档写进流。

        失败一律抛异常：调用方（worker）必须知道这条结论没出去。
        静默丢弃的代价是"结论凭空消失"，而排查时根本没有线索。
        """
        if not isinstance(message, dict):
            raise SinkPublishError(
                f"event must be a dict, got {type(message).__name__}")

        try:
            encoded = json.dumps(message, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            # 本地就失败，不要发半条出去：Redis 里留一条坏消息，
            # 读它的控制面会把它当毒消息丢掉，而真正的问题被藏得更深。
            raise SinkPublishError(
                f"event is not JSON serialisable: {exc}") from exc

        try:
            receipt = self._transport.xadd(self._stream, encoded, max_len=self._max_len)
        except SinkPublishError:
            raise
        except Exception as exc:
            raise SinkPublishError(
                f"failed to publish event to Redis stream {self._stream}: {exc}") from exc
        if receipt is None:
            raise SinkPublishError(
                f"XADD to {self._stream} returned no id; the event was NOT published")
        if not isinstance(receipt, (str, bytes)):
            raise SinkPublishError(
                f"XADD to {self._stream} returned an unexpected receipt: {receipt!r}")
