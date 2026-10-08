"""已完成的 run 被重复投递时，消息必须被 ACK 掉。

## 缺陷（我在 commit 546a8f8 里引入的）

546a8f8 修的是一个真问题：`min_idle_ms`(60s) < deadline(900s) 导致
正常进行中的调查会被 XAUTOCLAIM 抢走，而旧代码在认领失败时**无条件 ACK**，
于是"持有者崩溃 → 消息已被 ACK → 永久无人处理"。

那个修复是对的，但它把**两种完全不同的情况**当成了一种：

```python
if not self.ledger.claim(run_id):
    if not self.ledger.reclaimable(run_id, older_than_seconds=...):
        stats.skipped_duplicate += 1
        continue          # ← 不 ACK
```

`reclaimable()` 只在 `state == "IN_PROGRESS"` 时才可能为真。而
**已经跑完的 run（state == "DONE"）也走不进这个分支** ——
`reclaimable()` 对 DONE 返回 False，于是 `continue`，**不 ACK**。

## 为什么这是严重的

一个 DONE 的 run 再也不需要被处理了。消息留在 PEL 里的后果：

- 每次 XAUTOCLAIM 都会把它再投一次（min_idle_ms 之后），
  worker 每次都跳过它、都不 ACK —— **一条消息能无限重复投递**；
- PEL 永远不空，`pending` 计数只增不减，运维看到"消费者积压"
  却找不到任何在处理的消息；
- 真实 Redis 上验证过：`XCLAIM` 把 entry 给 c2 后，c2 的 `XACK`
  根本不会生效（entry 属于 c1 的 PEL），消息就钉在那儿了。

这正是 546a8f8 想避免的那类"消息丢失"的镜像：**消息卡死**。

## 正确行为

认领失败时必须区分三态：

| 账本状态 | 含义 | 动作 |
|---|---|---|
| `IN_PROGRESS` + 新鲜 | 别人正在做 | **不 ACK**（546a8f8 的修复） |
| `IN_PROGRESS` + 陈旧 | 持有者大概已死 | 接管并执行 |
| `DONE` | 已经跑完 | **ACK 掉**（本测试） |
"""
from __future__ import annotations

import pathlib
import tempfile

from traceops.runtime.run_ledger import RunLedger
from traceops.runtime.worker import (
    InMemoryEventSink,
    InMemoryEventSource,
    InvestigationWorker,
    derive_request_from_event,
)


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


def _worker(ledger: RunLedger) -> tuple[InvestigationWorker, InMemoryEventSource]:
    source, sink = InMemoryEventSource(), InMemoryEventSink()
    worker = InvestigationWorker(
        source=source, sink=sink,
        derive_request=derive_request_from_event,
        build_context=lambda r: None, ledger=ledger)
    return worker, source


class TestCompletedRunIsAcked:
    def test_a_finished_run_is_acked_when_redelivered(self, tmp_path):
        """已完成的 run 被重复投递 → 必须 ACK，否则消息永久留在 PEL。"""
        ledger = RunLedger(tmp_path / "l.db")
        ledger.claim("run-done")
        ledger.finish("run-done", outcome="COMPLETED")

        worker, source = _worker(ledger)
        source.push(_event("run-done"))
        stats = worker.poll_once()

        assert stats.skipped_duplicate >= 1, "the duplicate should be counted"
        assert "msg-run-done" in source.acked, (
            "a run that already finished was not acked; the message stays in the "
            "PEL forever and gets redelivered on every reclaim cycle")

    def test_the_same_holds_in_memory_mode(self):
        """内存模式（无持久化账本）也必须一致。

        内存模式的 `reclaimable()` 一律返回 False，所以 DONE 状态
        同样落进"不 ACK"分支 —— 两个模式都错才是真的系统性缺陷。
        """
        ledger = RunLedger()
        ledger.claim("run-done")
        ledger.finish("run-done", outcome="COMPLETED")

        worker, source = _worker(ledger)
        source.push(_event("run-done"))
        worker.poll_once()

        assert "msg-run-done" in source.acked, (
            "memory mode leaks the message the same way the SQLite mode does")

    def test_a_failed_run_is_also_acked_when_redelivered(self, tmp_path):
        """失败的 run 同样已经"处理过" —— 也不该留在 PEL 里。

        失败是终态。重试由上游策略决定（是否重新投递一条新消息），
        而不是靠 worker 把同一条消息永远留在 PEL 里。
        """
        ledger = RunLedger(tmp_path / "l.db")
        ledger.claim("run-failed")
        ledger.finish("run-failed", outcome="FAILED:BUDGET_EXHAUSTED")

        worker, source = _worker(ledger)
        source.push(_event("run-failed"))
        worker.poll_once()

        assert "msg-run-failed" in source.acked, (
            "a failed run is a terminal state; the message must be acked too")


class TestLiveHolderIsStillNotAcked:
    """546a8f8 的修复必须继续有效 —— 不能为了修 DONE 而破坏它。"""

    def test_a_run_being_processed_right_now_is_not_acked(self, tmp_path):
        ledger = RunLedger(tmp_path / "l.db")
        ledger.claim("run-live")  # 仍在进行中，从未 finish

        worker, source = _worker(ledger)
        source.push(_event("run-live"))
        stats = worker.poll_once()

        assert stats.skipped_duplicate >= 1
        assert "msg-run-live" not in source.acked, (
            "acking a message another worker is still processing loses it "
            "permanently if that worker dies")
