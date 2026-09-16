"""统一审计：把三处散落的审计合成一个可查询的流。

## 现状（PRD 5.7 的「审计中心」标为"散落各处"）

三份审计各自为政，形状、时间表示、失败语义都不同：

| 来源 | 字段 | 时间表示 | 记录什么 |
|---|---|---|---|
| `ToolGateway.audit_log()` | tool/outcome/params/error/duration_ms | `datetime` | 工具调用（含失败） |
| `RunLedger` | run_id/outcome/state/recorded_at | **ISO 字符串** | run 生命周期 |
| `DeadLetterRecord` | entry_id/stream/group/category/reason/raw | **ISO 字符串** | 坏消息 |

后果不是"不好看"，而是**无法回答真实问题**：

- "这次调查为什么失败？" 需要同时看工具调用失败 + run 状态 + 是否有死信
- 三份数据时间表示不同，**无法按时间线合并**
- `ToolGateway._audit` 是内存列表，进程重启即清空 —— 事后追查不可能

## 本模块的目标（最小可用）

- 统一成 `AuditEvent`：`kind` / `occurred_at`（**统一 datetime**）/ `subject` / `detail`
- 支持按时间窗、kind、subject 过滤
- **不假装有持久化**：内存实现即可，但要显式声明这个边界
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.evidence.audit import AuditCenter, AuditEvent, AuditKind


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC) + timedelta(seconds=seconds)


class TestAuditEventShape:
    def test_event_has_unified_time_type(self):
        event = AuditEvent(kind=AuditKind.TOOL_CALL, occurred_at=_ts(),
                           subject="query_service_logs", detail={"outcome": "NO_DATA"})
        assert isinstance(event.occurred_at, datetime), (
            "all sources must normalise to one time type, or timelines cannot merge")

    def test_event_is_frozen(self):
        """审计记录不可变 —— 事后修改审计等于销毁证据。"""
        event = AuditEvent(kind=AuditKind.TOOL_CALL, occurred_at=_ts(), subject="x")
        with pytest.raises(Exception):
            event.subject = "y"


class TestIngestFromGateway:
    def test_gateway_records_become_audit_events(self):
        events = AuditCenter.from_gateway_records([
            {"tool": "query_service_logs", "outcome": "OK", "params": {"service": "redis"},
             "error": None, "duration_ms": 12, "timestamp": _ts()},
        ])
        assert len(events) == 1
        assert events[0].kind is AuditKind.TOOL_CALL
        assert events[0].subject == "query_service_logs"
        assert events[0].detail["outcome"] == "OK"
        assert events[0].detail["duration_ms"] == 12

    def test_failed_tool_call_is_preserved(self):
        """失败调用必须进审计 —— 只审计成功等于没有审计。"""
        events = AuditCenter.from_gateway_records([
            {"tool": "query_service_traces", "outcome": "ERROR", "params": {},
             "error": "boom", "duration_ms": 5, "timestamp": _ts()},
        ])
        assert events[0].detail["outcome"] == "ERROR"
        assert events[0].detail["error"] == "boom"

    def test_string_timestamp_is_parsed(self):
        """gateway 用 datetime，但账本/死信用 ISO 字符串 —— 都要能进。"""
        events = AuditCenter.from_gateway_records([
            {"tool": "t", "outcome": "OK", "params": {}, "error": None,
             "duration_ms": 0, "timestamp": "2026-10-30T10:02:00+00:00"},
        ])
        assert events[0].occurred_at == _ts()


class TestIngestFromDeadLetter:
    def test_dead_letter_becomes_audit_event(self):
        from traceops.runtime.deadletter import DeadLetterRecord

        record = DeadLetterRecord(
            original_entry_id="1-0", stream="traceops.events", group="g",
            consumer="c", category="INVALID_REQUEST", reason="missing fields",
            raw="{}", occurred_at="2026-10-30T10:02:00+00:00",
        )
        event = AuditCenter.from_dead_letter(record)
        assert event.kind is AuditKind.DEAD_LETTER
        assert event.subject == "traceops.events"
        assert event.detail["category"] == "INVALID_REQUEST"
        assert event.occurred_at == _ts()


class TestIngestFromLedger:
    def test_ledger_rows_become_audit_events(self):
        rows = [{"run_id": "run-1", "outcome": "COMPLETED", "state": "DONE",
                 "recorded_at": "2026-10-30T10:02:00+00:00"}]
        events = AuditCenter.from_ledger_rows(rows)
        assert len(events) == 1
        assert events[0].kind is AuditKind.RUN_OUTCOME
        assert events[0].subject == "run-1"
        assert events[0].detail["outcome"] == "COMPLETED"


class TestUnifiedTimeline:
    def test_all_sources_merge_by_time(self):
        """真正的价值：三类事件必须能按时间线合并排序。"""
        center = AuditCenter()
        center.add_all(AuditCenter.from_gateway_records([
            {"tool": "q1", "outcome": "OK", "params": {}, "error": None,
             "duration_ms": 1, "timestamp": _ts(10)}]))
        center.add_all(AuditCenter.from_ledger_rows([
            {"run_id": "run-1", "outcome": "COMPLETED", "state": "DONE",
             "recorded_at": _ts(20).isoformat()}]))

        timeline = center.timeline()
        assert len(timeline) == 2
        assert timeline[0].occurred_at < timeline[1].occurred_at
        assert [e.kind for e in timeline] == [AuditKind.TOOL_CALL, AuditKind.RUN_OUTCOME]

    def test_filter_by_kind(self):
        center = AuditCenter()
        center.add_all(AuditCenter.from_gateway_records([
            {"tool": "q1", "outcome": "OK", "params": {}, "error": None,
             "duration_ms": 1, "timestamp": _ts(10)}]))
        center.add_all(AuditCenter.from_ledger_rows([
            {"run_id": "run-1", "outcome": "COMPLETED", "state": "DONE",
             "recorded_at": _ts(20).isoformat()}]))
        assert len(list(center.timeline(kind=AuditKind.RUN_OUTCOME))) == 1

    def test_filter_by_time_window(self):
        center = AuditCenter()
        center.add_all(AuditCenter.from_gateway_records([
            {"tool": "q1", "outcome": "OK", "params": {}, "error": None,
             "duration_ms": 1, "timestamp": _ts(0)},
            {"tool": "q2", "outcome": "OK", "params": {}, "error": None,
             "duration_ms": 1, "timestamp": _ts(100)}]))
        window = list(center.timeline(since=_ts(50)))
        assert len(window) == 1 and window[0].subject == "q2"

    def test_filter_by_subject(self):
        center = AuditCenter()
        center.add_all(AuditCenter.from_gateway_records([
            {"tool": "q1", "outcome": "OK", "params": {}, "error": None,
             "duration_ms": 1, "timestamp": _ts(0)},
            {"tool": "q2", "outcome": "OK", "params": {}, "error": None,
             "duration_ms": 1, "timestamp": _ts(1)}]))
        assert len(list(center.timeline(subject="q1"))) == 1

    def test_timeline_is_sorted_even_if_added_out_of_order(self):
        center = AuditCenter()
        center.add(AuditEvent(kind=AuditKind.TOOL_CALL, occurred_at=_ts(30), subject="late"))
        center.add(AuditEvent(kind=AuditKind.TOOL_CALL, occurred_at=_ts(10), subject="early"))
        assert [e.subject for e in center.timeline()] == ["early", "late"]


class TestInvestigationStory:
    """"这次调查为什么失败" —— 需要三源合看才能回答。"""

    def test_failure_story_spans_all_sources(self):
        from traceops.runtime.deadletter import DeadLetterRecord

        center = AuditCenter()
        center.add_all(AuditCenter.from_gateway_records([
            {"tool": "query_service_logs", "outcome": "ERROR", "params": {},
             "error": "datasource unreachable", "duration_ms": 3, "timestamp": _ts(5)}]))
        center.add(AuditCenter.from_dead_letter(DeadLetterRecord(
            original_entry_id="1-0", stream="traceops.events", group="g",
            consumer="c", category="INVALID_REQUEST", reason="bad payload",
            raw="{}", occurred_at=_ts(6).isoformat())))
        center.add_all(AuditCenter.from_ledger_rows([
            {"run_id": "run-9", "outcome": "FAILED:INTERNAL", "state": "DONE",
             "recorded_at": _ts(7).isoformat()}]))

        story = center.timeline()
        kinds = [e.kind for e in story]
        assert AuditKind.TOOL_CALL in kinds
        assert AuditKind.DEAD_LETTER in kinds
        assert AuditKind.RUN_OUTCOME in kinds


class TestLedgerExposesRowsForAudit:
    """账本必须能导出记录行，否则审计中心无法合并它的数据。

    此前 RunLedger 只有 is_seen / claim_state / outcome_for 三个单点查询，
    没有任何"列出全部"的能力 —— 时间线就永远缺 run 这一维。
    """

    def test_ledger_can_list_rows(self, tmp_path):
        from traceops.runtime.run_ledger import RunLedger

        ledger = RunLedger(tmp_path / "l.db")
        ledger.claim("run-1")
        ledger.finish("run-1", outcome="COMPLETED")

        rows = ledger.rows()
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run-1"
        assert rows[0]["outcome"] == "COMPLETED"
        assert rows[0]["state"] == "DONE"
        assert rows[0]["recorded_at"]

    def test_memory_mode_also_lists_rows(self):
        from traceops.runtime.run_ledger import RunLedger

        ledger = RunLedger()
        ledger.claim("run-1")
        rows = ledger.rows()
        assert len(rows) == 1 and rows[0]["run_id"] == "run-1"

    def test_rows_feed_the_audit_center(self, tmp_path):
        from traceops.runtime.run_ledger import RunLedger

        ledger = RunLedger(tmp_path / "l2.db")
        ledger.claim("run-1")
        ledger.finish("run-1", outcome="FAILED:INTERNAL")
        events = AuditCenter.from_ledger_rows(ledger.rows())
        assert len(events) == 1
        assert events[0].detail["outcome"] == "FAILED:INTERNAL"


class TestWorkerAuditTimeline:
    """worker 必须能直接产出"这次调查的完整时间线"。

    这是审计中心的实用价值：调用方不需要知道三份数据在哪、什么形状，
    只要问 worker 要时间线。
    """

    def test_worker_builds_timeline_from_all_sources(self):
        from traceops.runtime.run_ledger import RunLedger
        from traceops.runtime.worker import InvestigationWorker, InMemoryEventSink, InMemoryEventSource

        ledger = RunLedger()
        ledger.claim("run-1")
        ledger.finish("run-1", outcome="COMPLETED")

        worker = InvestigationWorker(
            source=InMemoryEventSource(), sink=InMemoryEventSink(),
            derive_request=lambda m: None, build_context=lambda r: None,
            ledger=ledger,
        )
        timeline = worker.audit_timeline()
        assert len(timeline) == 1
        assert timeline[0].kind is AuditKind.RUN_OUTCOME
        assert timeline[0].subject == "run-1"
