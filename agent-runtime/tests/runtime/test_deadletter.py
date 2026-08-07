"""死信流（Dead Letter Stream）测试。

## 为什么必须有死信流

坏消息有两种处理方式，都不对：
1. ACK 掉 → 数据静默消失，事后无法审计"哪些事件被丢弃了"
2. 不 ACK → 永远滞留 PEL，占满 pending，且反复被认领浪费时间

正确做法：坏消息**转移到独立死信流**并附失败原因，然后从主流感 ACK 掉。
主流程不被阻塞，同时保留完整审计。

死信流不是"垃圾桶"：它必须带可聚合的失败原因枚举（与 contracts 的
failure_reason 同构），否则无法统计"哪类坏消息最多"。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from traceops.runtime.deadletter import (
    DeadLetterRecord,
    DeadLetterSink,
    FailureCategory,
    InMemoryDeadLetterSink,
)

TS = "2026-10-30T10:06:00Z"


class TestFailureCategory:
    def test_categories_are_enum_not_free_text(self):
        """自由文本无法聚合统计 —— 必须是枚举。"""
        assert FailureCategory.MALFORMED_JSON.value == "MALFORMED_JSON"
        assert FailureCategory.INVALID_REQUEST.value == "INVALID_REQUEST"
        assert FailureCategory.MISSING_ENVELOPE.value == "MISSING_ENVELOPE"
        assert FailureCategory.UNKNOWN.value == "UNKNOWN"

    def test_classify_missing_envelope(self):
        from traceops.runtime.deadletter import classify_failure
        assert classify_failure('payload missing required fields: [\'service_hint\']') \
            is FailureCategory.INVALID_REQUEST

    def test_classify_json_error(self):
        from traceops.runtime.deadletter import classify_failure
        assert classify_failure("Expecting property name enclosed in double quotes") \
            is FailureCategory.MALFORMED_JSON

    def test_classify_unknown_falls_back(self):
        from traceops.runtime.deadletter import classify_failure
        assert classify_failure("something weird happened") is FailureCategory.UNKNOWN


class TestDeadLetterRecord:
    def test_record_carries_original_entry_id(self):
        """原始 entry_id 必须保留 —— 没有它就无法回溯主流里是哪条。"""
        record = DeadLetterRecord(
            original_entry_id="1700000000000-1",
            stream="traceops.events",
            group="traceops-workers",
            consumer="worker-1",
            category=FailureCategory.MALFORMED_JSON,
            reason="Expecting value: line 1 column 1",
            raw='{"broken"',
            occurred_at=TS,
        )
        assert record.original_entry_id == "1700000000000-1"
        assert record.category is FailureCategory.MALFORMED_JSON

    def test_record_is_serialisable(self):
        record = DeadLetterRecord(
            original_entry_id="1-0", stream="s", group="g", consumer="c",
            category=FailureCategory.INVALID_REQUEST, reason="missing field",
            raw="{}", occurred_at=TS,
        )
        payload = record.to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["category"] == "INVALID_REQUEST"

    def test_raw_is_truncated_to_bound_size(self):
        """坏消息原文可能极大（甚至恶意），必须截断。"""
        record = DeadLetterRecord(
            original_entry_id="1-0", stream="s", group="g", consumer="c",
            category=FailureCategory.MALFORMED_JSON, reason="too long",
            raw="x" * 10_000, occurred_at=TS,
        )
        assert len(record.raw) <= DeadLetterRecord.MAX_RAW_CHARS
        assert record.truncated is True

    def test_short_raw_not_marked_truncated(self):
        record = DeadLetterRecord(
            original_entry_id="1-0", stream="s", group="g", consumer="c",
            category=FailureCategory.UNKNOWN, reason="r",
            raw="small", occurred_at=TS,
        )
        assert record.truncated is False


class TestInMemorySink:
    def test_sink_records_records(self):
        sink = InMemoryDeadLetterSink()
        record = DeadLetterRecord(
            original_entry_id="1-0", stream="s", group="g", consumer="c",
            category=FailureCategory.MALFORMED_JSON, reason="bad", raw="{", occurred_at=TS,
        )
        sink.send(record)
        assert sink.records == [record]

    def test_count_by_category(self):
        sink = InMemoryDeadLetterSink()
        for category in (FailureCategory.MALFORMED_JSON, FailureCategory.MALFORMED_JSON,
                         FailureCategory.INVALID_REQUEST):
            sink.send(DeadLetterRecord(
                original_entry_id="1-0", stream="s", group="g", consumer="c",
                category=category, reason="r", raw="r", occurred_at=TS))
        counts = sink.count_by_category()
        assert counts[FailureCategory.MALFORMED_JSON] == 2
        assert counts[FailureCategory.INVALID_REQUEST] == 1


class TestProtocol:
    def test_sink_protocol_shape(self):
        """DeadLetterSink 必须满足协议，便于换成 Redis Streams 实现。

        注意：运行时 `isinstance` 对 Protocol 不可靠（除非 @runtime_checkable），
        所以这里检查真正重要的东西——方法签名兼容。
        """
        sink = InMemoryDeadLetterSink()
        assert callable(getattr(sink, "send"))
        import inspect
        params = list(inspect.signature(sink.send).parameters)
        assert params == ["record"]


class TestWorkerDeadLetterIntegration:
    """坏消息经 worker 时真的进死信流，而不是被静默吞掉。"""

    def _worker(self, sink_records):
        from datetime import UTC, datetime
        from traceops.runtime.deadletter import InMemoryDeadLetterSink
        from traceops.runtime.worker import (
            InMemoryEventSink,
            InMemoryEventSource,
            InvestigationWorker,
            derive_request_from_event,
        )

        source = InMemoryEventSource()
        sink = InMemoryEventSink()
        dead = InMemoryDeadLetterSink()
        worker = InvestigationWorker(
            source=source,
            sink=sink,
            derive_request=derive_request_from_event,
            build_context=lambda request: (_ for _ in ()).throw(
                AssertionError("must not build context for invalid message")),
            dead_letters=dead,
        )
        return worker, source, sink, dead

    def test_invalid_message_lands_in_dead_letter_sink(self):
        worker, source, sink, dead = self._worker(None)
        source.push({"id": "1-0", "payload": {"payload": {"incident_id": "inc-x"}}})

        stats = worker.poll_once()

        assert stats.failed == 1
        assert len(dead.records) == 1
        record = dead.records[0]
        assert record.original_entry_id == "1-0"
        assert record.category is FailureCategory.INVALID_REQUEST
        # 主流仍然 ACK 掉，避免坏消息滞留 PEL
        assert source.acked == ["1-0"]

    def test_dead_letter_reason_is_populated(self):
        worker, source, sink, dead = self._worker(None)
        source.push({"id": "2-0", "payload": {"payload": {"incident_id": "inc-x"}}})
        worker.poll_once()
        assert "service_hint" in dead.records[0].reason

    def test_no_dead_letter_sink_configured_does_not_crash(self):
        """未配置死信流时不能崩 —— 但这是在依赖调用方的决定，文档里必须写明。"""
        from traceops.runtime.worker import (
            InMemoryEventSink,
            InMemoryEventSource,
            InvestigationWorker,
            derive_request_from_event,
        )
        source = InMemoryEventSource()
        worker = InvestigationWorker(
            source=source, sink=InMemoryEventSink(), derive_request=derive_request_from_event,
            build_context=lambda r: None, dead_letters=None)
        source.push({"id": "3-0", "payload": {"payload": {}}})
        stats = worker.poll_once()
        assert stats.failed == 1
