"""被抢占的消息不能因为"另一个 worker 已认领"而**丢失处理**。

## 缺陷推演（搜索 XAUTOCLAIM 实践后确认风险模型）

`min_idle_ms` 默认 60 秒，但 `deadline_seconds` 默认 900 秒 ——
一个正常的长调查（有慢数据源）会被 `XAUTOCLAIM` **抢走**。

当前 `poll_once` 的处理是：

```python
if not self.ledger.claim(run_id):
    stats.skipped_duplicate += 1
    self.source.ack(message["id"])      # ← 认领失败也 ACK
    continue
```

推演出的丢失路径：

1. worker-1 认领 run-X，开始跑（预计 200 秒）
2. 60 秒后 worker-2 的 XAUTOCLAIM 抢到这条消息
3. worker-2 `claim()` 失败（worker-1 持有）→ **ACK 掉**
4. worker-1 在 200 秒时崩溃，从未完成
5. 消息已被 ACK，**没有 worker 会再处理它** → 事故永久停摆

第 3 步是关键：**"别人正在做"不等于"已经做完了"**。
认领失败时应该**不 ACK**，让消息回到原消费者的 PEL，等它真的完成或真的死掉。

## 但也不能永不 ACK

若持有者真的死了，消息会永远留在 PEL。需要一个判据区分
"别人正在做"与"别人的认领是陈旧的"。`RunLedger` 已有 `reclaimable()`
用于判断陈旧认领 —— 复用同一判据。

## 三条约束

1. 新鲜认领（别人正在做）→ **不 ACK**，跳过本次
2. 陈旧认领（持有者大概率已死）→ 允许接管
3. 无论哪种，都必须有可观测计数（否则静默跳过不可排障）
"""
from __future__ import annotations

import pytest

from traceops.runtime.run_ledger import RunLedger
from traceops.runtime.worker import InvestigationWorker, InMemoryEventSink, InMemoryEventSource


def _event(run_id: str) -> dict:
    return {
        "id": f"msg-{run_id}",
        "payload": {
            "envelope": {
                "event_id": "evt-" + run_id, "event_type": "investigation.requested",
                "schema_version": "1.0", "occurred_at": "2026-10-30T10:02:00Z",
                "producer": "control-plane", "correlation_id": "inc-1",
                "aggregate_id": run_id, "aggregate_version": 1, "causation_id": None,
            },
            "payload": {
                "investigation_run_id": run_id, "incident_id": "inc-1",
                "service_hint": "order-service", "environment": "demo",
                "config_version": "cfg",
                "time_range": {"start": "2026-10-30T10:00:00Z",
                               "end": "2026-10-30T10:06:00Z"},
            },
        },
    }


class TestClaimedElsewhereIsNotAcked:
    def test_message_held_by_another_worker_is_left_unacked(self):
        """别人正在做 → 不 ACK，消息留在 PEL，等持有者完成或死亡。

        ACK 掉会让这条事故在持有者崩溃后**永久无人处理**。
        """
        import tempfile, pathlib
        ledger = RunLedger(pathlib.Path(tempfile.mkdtemp()) / "l.db")
        # 模拟 worker-1 已认领 run-A 且仍在进行（新鲜认领）
        assert ledger.claim("run-A") is True

        from traceops.runtime.worker import derive_request_from_event

        source, sink = InMemoryEventSource(), InMemoryEventSink()
        worker = InvestigationWorker(
            source=source, sink=sink,
            derive_request=derive_request_from_event,
            build_context=lambda r: None, ledger=ledger)

        source.push(_event("run-A"))
        stats = worker.poll_once()

        assert stats.skipped_duplicate >= 1, "should be counted as a duplicate"
        # 关键：消息**没有**被 ACK
        assert "msg-run-A" not in source.acked, (
            "acking a message another worker is still processing loses it forever "
            "if that worker dies")

    def test_stale_claim_is_reclaimable(self, tmp_path):
        """持有者死掉后（认领陈旧），消息必须能被接管。

        **必须用持久化模式**：内存模式无法表达时间，无法判断陈旧。
        """
        ledger = RunLedger(tmp_path / "l.db")
        assert ledger.claim("run-B") is True
        # 阈值为 0 时任何年龄都算陈旧 → 可接管
        assert ledger.reclaimable("run-B", older_than_seconds=0) is True

    def test_fresh_claim_is_not_reclaimable(self, tmp_path):
        """**持久化模式**下才可能判断年龄；内存模式无法表达时间。

        这条刻意用 SQLite：生产用的就是它。内存模式保守返回 True 是
        已知局限（进程重启本就清空内存），写在这里以免被误当成 bug。
        """
        ledger = RunLedger(tmp_path / "l.db")
        assert ledger.claim("run-C") is True
        assert ledger.reclaimable("run-C", older_than_seconds=3600) is False

    def test_observability_counter_exists(self):
        """跳过的消息必须有计数 —— 否则静默跳过不可排障。"""
        ledger = RunLedger()
        ledger.claim("run-D")
        from traceops.runtime.worker import derive_request_from_event

        source = InMemoryEventSource()
        worker = InvestigationWorker(
            source=source, sink=InMemoryEventSink(),
            derive_request=derive_request_from_event,
            build_context=lambda r: None, ledger=ledger)
        source.push(_event("run-D"))
        stats = worker.poll_once()
        assert stats.skipped_duplicate >= 1


class TestConfigIsActuallyWired:
    """配置必须真的传进 worker —— 否则是又一个"有参数但不起作用"。"""

    def test_worker_receives_stale_claim_seconds(self):
        from traceops.runtime.worker import InvestigationWorker, InMemoryEventSink, InMemoryEventSource

        worker = InvestigationWorker(
            source=InMemoryEventSource(), sink=InMemoryEventSink(),
            derive_request=lambda m: None, build_context=lambda r: None,
            stale_claim_seconds=123)
        assert worker.stale_claim_seconds == 123

    def test_default_is_not_shorter_than_the_investigation_deadline(self):
        """接管阈值必须 >= 调查上限，否则会**误抢正在正常处理**的消息。

        这是本缺陷的根源：min_idle_ms=60s 小于 deadline=900s，
        于是一个正常的慢调查会被别人抢走。
        """
        from traceops.runtime.budget import BudgetManager
        from traceops.runtime.worker import InvestigationWorker, InMemoryEventSink, InMemoryEventSource

        worker = InvestigationWorker(
            source=InMemoryEventSource(), sink=InMemoryEventSink(),
            derive_request=lambda m: None, build_context=lambda r: None)
        assert worker.stale_claim_seconds >= BudgetManager().deadline_seconds, (
            "taking over a claim sooner than the investigation deadline means "
            "stealing runs that are still progressing normally")
