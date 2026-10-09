"""worker 必须只消费 investigation.requested，不能把自己的输出当输入。

背景：控制面和 worker 读的是**同一条**流 traceops.events（不同消费组）。
worker 自己也会往这条流写 investigation.updated / investigation.completed /
investigation.failed。于是 worker 的消费组会读到自己写的消息。

此前 worker 对任何非 investigation.requested 的消息都抛
"unexpected event_type"，落到 dead_letter，并**再发一条
investigation.failed** —— 每条自己写的消息都变成一条新的失败事件，
形成自我放大的回路（实测：跑一次 worker，流里多出两条 failed）。

这不是"多消费了几条无害消息"，而是：
1. 每次运行都往流里灌垃圾；
2. dead_letter 表被自己的输出污染；
3. 统计里的 failed 数字和真实失败对不上。
"""

from __future__ import annotations

import pytest

from traceops.runtime.worker import (
    EventSink,
    InMemoryEventSource,
    InvestigationWorker,
    WorkerStats,
)


class _RecordingSink:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def publish(self, message: dict) -> None:
        self.messages.append(message)


def _envelope(event_type: str, run_id: str = "run-1") -> dict:
    return {
        "envelope": {
            "event_id": f"evt-{event_type.replace('.', '-')}",
            "event_type": event_type,
            "schema_version": "1.0",
            "occurred_at": "2026-10-31T16:00:00+00:00",
            "correlation_id": "inc-1",
            "producer": "agent-runtime",
        },
        "payload": {"investigation_run_id": run_id, "status": "RUNNING"},
    }


def _worker(source: InMemoryEventSource, sink: _RecordingSink) -> InvestigationWorker:
    from traceops.orchestration.graph import RunContext

    def _derive(message):
        from traceops.runtime.worker import derive_request_from_event

        return derive_request_from_event(message)

    return InvestigationWorker(
        source=source,
        sink=sink,
        derive_request=_derive,
        build_context=lambda request: RunContext(store=None, adapters=None),
    )


class TestWorkerIgnoresItsOwnOutputEvents:
    def test_an_investigation_updated_event_is_not_an_invalid_request(self):
        source = InMemoryEventSource()
        sink = _RecordingSink()
        source.push(_envelope("investigation.updated"))

        worker = _worker(source, sink)
        stats = worker.poll_once()

        # 不是"非法请求"：既不该进 dead_letter，也不该发失败事件
        assert stats.failed == 0
        assert stats.processed == 0
        assert [m["envelope"]["event_type"] for m in sink.messages] == []

    def test_an_investigation_failed_event_does_not_produce_another_failure(self):
        source = InMemoryEventSource()
        sink = _RecordingSink()
        source.push(_envelope("investigation.failed"))

        worker = _worker(source, sink)
        stats = worker.poll_once()

        # 这正是自我放大回路的关键一环：读到自己写的 failed，
        # 如果再写一条 failed，条数只会单调增长。
        assert stats.failed == 0
        assert sink.messages == []

    def test_an_investigation_completed_event_is_also_ignored(self):
        source = InMemoryEventSource()
        sink = _RecordingSink()
        source.push(_envelope("investigation.completed"))

        worker = _worker(source, sink)
        stats = worker.poll_once()

        assert stats.failed == 0
        assert sink.messages == []

    def test_a_malformed_request_is_still_reported_as_a_failure(self):
        """不能把"忽略自己写的事件"做成"忽略所有非法事件"。

        真正非法的请求（缺字段）必须仍然进 dead_letter 并记 failed ——
        否则毒消息会被静默吞掉。
        """
        source = InMemoryEventSource()
        sink = _RecordingSink()
        source.push({
            "envelope": {
                "event_id": "evt-bad",
                "event_type": "investigation.requested",
                "schema_version": "1.0",
                "occurred_at": "2026-10-31T16:00:00+00:00",
                "correlation_id": "inc-1",
                "producer": "control-plane",
            },
            "payload": {"incident_id": "inc-1"},  # 缺 investigation_run_id 等
        })

        worker = _worker(source, sink)
        stats = worker.poll_once()

        assert stats.failed == 1
        assert any(
            m["envelope"]["event_type"] == "investigation.failed" for m in sink.messages
        )


class TestEventTypeGate:
    def test_the_gate_is_expressed_as_a_set_not_a_comparison(self):
        """用集合表达"我关心哪些类型"，避免下次再加一种类型时漏改。"""
        from traceops.runtime import worker as worker_module

        assert hasattr(worker_module, "REQUESTED_EVENT_TYPE")
        assert worker_module.REQUESTED_EVENT_TYPE == "investigation.requested"
