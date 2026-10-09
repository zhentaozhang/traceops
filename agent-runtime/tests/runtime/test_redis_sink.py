"""Python 侧的 Redis 发布端 —— 此前**根本不存在**。

背景：`--mode redis` 的 worker 能从 Redis **读** investigation.requested，
但它产出的 investigation.completed / investigation.failed 只进了内存列表，
然后被打印到 stdout。结果是：

  Java ──XADD──▶ Redis ──▶ Python worker ──(只打印)──▶ ✗ 控制面收不到结论

控制面因此永远看不到终态事件：既拿不到诊断结论，也拿不到 `retryable`，
重试循环的第一环（"失败事件回到控制面"）在真实传输上从未发生。
`架构说明` 的图里 Python 平面**没有回指 Redis 的箭头**——
那不是画漏了，是链路真的没有。

这个模块要做的就是补上写端：XADD traceops.events * payload <json>，
字段名与 Java 侧 `RedisStreamEventPublisher` 完全一致（"payload"），
这样一条消息从 Java 发出、被 agent-runtime 以同样形状写回时，
两侧的 reader 用同一套解析代码即可，不必各写一份。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from traceops.runtime.redis_sink import RedisStreamEventSink, SinkPublishError


class _RecordingTransport:
    """记录 XADD 调用，不碰网络。"""

    def __init__(self, reply: Any = "1-1") -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.reply = reply

    def xadd(self, stream: str, payload: str, *, max_len: int | None = None) -> Any:
        self.calls.append((stream, payload, max_len))
        return self.reply


class TestRedisStreamEventSink:
    def test_publish_writes_to_the_stream_with_the_payload_field(self) -> None:
        transport = _RecordingTransport()
        sink = RedisStreamEventSink(transport, stream="traceops.events")

        sink.publish({"envelope": {"event_type": "investigation.completed"},
                      "payload": {"investigation_run_id": "run-1"}})

        assert len(transport.calls) == 1
        stream, payload, _max_len = transport.calls[0]
        assert stream == "traceops.events"
        # 写进去的必须是**完整文档**（envelope + payload），
        # 因为 Java 侧的 reader 期望 entry 的 payload field 里装着整份事件。
        assert json.loads(payload) == {
            "envelope": {"event_type": "investigation.completed"},
            "payload": {"investigation_run_id": "run-1"},
        }

    def test_publish_uses_the_same_field_name_as_the_java_writer(self) -> None:
        """字段名必须与 Java 侧 RedisStreamEventPublisher 一致（"payload"）。

        这不是风格问题：两侧 reader 各自按字段名取值，
        名字不一致的后果是 Python 写的事件 Java 读成空 payload，
        然后被当成"无法使用的消息"丢进毒消息分支 —— 静默失败。
        """
        transport = _RecordingTransport()
        sink = RedisStreamEventSink(transport, stream="traceops.events")

        sink.publish({"envelope": {"event_type": "investigation.failed"}, "payload": {}})

        _stream, payload, _max_len = transport.calls[0]
        written = json.loads(payload)
        assert "envelope" in written
        assert "payload" in written

    def test_a_missing_xadd_receipt_is_an_error_not_a_silent_success(self) -> None:
        """XADD 没有回执 = 事件没发出去。

        静默成功比抛异常糟糕得多：调用方以为已经投递，
        结论就永远停在 agent 这一侧，而控制面那边只是"一直没有事件"。
        """
        transport = _RecordingTransport(reply=None)
        sink = RedisStreamEventSink(transport, stream="traceops.events")

        with pytest.raises(SinkPublishError):
            sink.publish({"envelope": {"event_type": "investigation.completed"},
                          "payload": {}})

    def test_publish_reports_the_stream_and_the_underlying_reason(self) -> None:
        transport = _RecordingTransport(reply=None)
        sink = RedisStreamEventSink(transport, stream="traceops.events")

        with pytest.raises(SinkPublishError) as excinfo:
            sink.publish({"envelope": {}, "payload": {}})

        message = str(excinfo.value)
        assert "traceops.events" in message

    def test_a_message_that_is_not_json_serialisable_is_rejected_before_sending(self) -> None:
        """不可序列化的消息在**本地**就失败，不要发半条出去。"""
        transport = _RecordingTransport()
        sink = RedisStreamEventSink(transport, stream="traceops.events")

        with pytest.raises(SinkPublishError):
            sink.publish({"envelope": {"event_type": "x"}, "payload": {"bad": object()}})

        assert transport.calls == []


class TestSinkMatchesTheEventSinkProtocol:
    def test_it_satisfies_the_worker_event_sink_protocol(self) -> None:
        """worker.py 的 EventSink Protocol 只有 publish() 一个方法。

        显式断言一次：以后谁给协议加了方法，这个 sink 必须跟着改，
        否则运行时才炸（而且是在线上）。
        """
        from traceops.runtime.worker import EventSink

        sink = RedisStreamEventSink(_RecordingTransport(), stream="traceops.events")
        assert isinstance(sink, EventSink)
