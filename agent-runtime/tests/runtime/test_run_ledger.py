"""Run 账本测试：幂等必须跨进程重启有效。

## 缺口

现有 `_known_run_ids` 是进程内的 set。进程重启后它清空，于是 Redis 里
未被 ACK 的消息（或上游重发的消息）会被当成新消息，**同一个 run 被重复调查**。

重复调查的代价不只是钱：两次调查可能查到不同时刻的数据、给出不同结论，
然后两条结果都流向 Control Plane（虽然那边有 event_id 去重，但 Python 侧
已经白跑一遍）。

## 做法

用 SQLite 持久化账本（与 EvidenceStore 同思路，标准库无依赖）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from traceops.runtime.run_ledger import RunLedger


class TestInMemoryLedger:
    def test_records_and_detects(self):
        ledger = RunLedger()
        assert ledger.is_seen("run-1") is False
        ledger.record("run-1", outcome="COMPLETED")
        assert ledger.is_seen("run-1") is True

    def test_outcome_is_retrievable(self):
        ledger = RunLedger()
        ledger.record("run-1", outcome="FAILED")
        assert ledger.outcome_for("run-1") == "FAILED"

    def test_unknown_run_has_no_outcome(self):
        assert RunLedger().outcome_for("nope") is None

    def test_recording_twice_updates_outcome(self):
        """同一 run 重试成功后应更新为最终结果，而不是保留第一次失败。"""
        ledger = RunLedger()
        ledger.record("run-1", outcome="FAILED")
        ledger.record("run-1", outcome="COMPLETED")
        assert ledger.outcome_for("run-1") == "COMPLETED"


class TestPersistentLedger:
    def test_seen_survives_restart(self, tmp_path: Path):
        """核心断言：重启后仍能识别"这个 run 处理过"。"""
        db = tmp_path / "runs.db"

        ledger = RunLedger(path=db)
        ledger.record("run-1", outcome="COMPLETED")

        reopened = RunLedger(path=db)
        assert reopened.is_seen("run-1") is True
        assert reopened.outcome_for("run-1") == "COMPLETED"

    def test_unseen_run_after_restart(self, tmp_path: Path):
        db = tmp_path / "runs.db"
        RunLedger(path=db).record("run-1", outcome="COMPLETED")

        reopened = RunLedger(path=db)
        assert reopened.is_seen("run-2") is False

    def test_tampered_nothing_but_counts_survive(self, tmp_path: Path):
        db = tmp_path / "runs.db"
        ledger = RunLedger(path=db)
        for i in range(5):
            ledger.record(f"run-{i}", outcome="COMPLETED")

        reopened = RunLedger(path=db)
        assert reopened.count() == 5

    def test_no_path_keeps_memory_behaviour(self):
        ledger = RunLedger()
        ledger.record("run-1", outcome="COMPLETED")
        assert ledger.count() == 1


class TestWorkerUsesPersistentLedger:
    """worker 必须用账本判断，而不是自己的内存 set。"""

    def test_reprocessed_run_is_skipped_after_restart(self, tmp_path: Path):
        from traceops.runtime.worker import (
            InMemoryEventSink, InMemoryEventSource, InvestigationWorker,
            derive_request_from_event,
        )

        db = tmp_path / "runs.db"
        event = {
            "id": "1-0",
            "payload": {
                "envelope": {
                    "event_id": "evt-x", "event_type": "investigation.requested",
                    "schema_version": "1.0", "occurred_at": "2026-10-30T10:06:00Z",
                    "correlation_id": "inc-1", "producer": "control-plane",
                },
                "payload": {
                    "investigation_run_id": "run-ledger",
                    "incident_id": "inc-1", "service_hint": "order-service",
                    "environment": "demo",
                    "time_range": {"start": "2026-10-30T10:00:00Z",
                                   "end": "2026-10-30T10:06:00Z"},
                    "config_version": "cfg",
                },
            },
        }

        first = InvestigationWorker(
            source=InMemoryEventSource(), sink=InMemoryEventSink(),
            derive_request=derive_request_from_event,
            build_context=lambda r: (_ for _ in ()).throw(AssertionError("should not run")),
            ledger=RunLedger(path=db, preload=False),
        )
        # 先标记为已处理
        first.ledger.record("run-ledger", outcome="COMPLETED")

        # 模拟重启：新 worker、同一 db
        source = InMemoryEventSource()
        source.push(event)
        worker = InvestigationWorker(
            source=source, sink=InMemoryEventSink(),
            derive_request=derive_request_from_event,
            build_context=lambda r: (_ for _ in ()).throw(AssertionError("must not run")),
            ledger=RunLedger(path=db),
        )
        stats = worker.poll_once()

        assert stats.skipped_duplicate == 1, "restart must still dedupe"
