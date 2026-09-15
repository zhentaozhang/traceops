"""Run 账本必须能**原子地认领**一个 run，而不只是"查一下有没有见过"。

## 缺陷（并发 worker 场景）

`worker.poll_once` 现在这样用账本：

```python
already_seen = run_id in self._known_run_ids or self.ledger.is_seen(run_id)
if already_seen: ... continue
# ... 开始执行调查（可能耗时数十秒）...
self.ledger.record(run.run_id, outcome="COMPLETED")   # 最后才写
```

`is_seen` 检查与 `record` 写入之间有**整个调查的时长**。两个 worker
并行消费同一条消息（Redis Streams 的 XAUTOCLAIM 就会造成这种情况：
一个 worker 崩了，另一个抢回 pending 消息）时：

```
worker A: is_seen(run-1) -> False   ─┐
worker B: is_seen(run-1) -> False   ─┤ 两个都通过
worker A: 调查中...                  │
worker B: 调查中...                  │  重复调查同一 run
worker A: record("COMPLETED")        │
worker B: record("COMPLETED")        ┘
```

真实代价：重复调用工具（花 token/配额）、重复发事件（下游看到两次结论）、
证据被写两遍。

## 正确行为

需要一个**认领（claim）**语义：检查和占用必须是同一步。
- 第一个 worker 认领成功 → 执行
- 第二个 worker 认领失败 → 跳过（并且应当能区分"别人正在跑"与"已经跑完"）
"""
from __future__ import annotations

import threading
import time

import pytest

from traceops.runtime.run_ledger import RunLedger


class TestClaim:
    def test_first_claim_succeeds(self):
        ledger = RunLedger()
        assert ledger.claim("run-1") is True

    def test_second_claim_fails(self):
        ledger = RunLedger()
        assert ledger.claim("run-1") is True
        assert ledger.claim("run-1") is False

    def test_claim_distinguishes_in_progress_from_done(self):
        """认领失败时要能说出"别人在跑"还是"已经跑完" —— 两者处理不同。"""
        ledger = RunLedger()
        ledger.claim("run-1")
        assert ledger.claim_state("run-1") == "IN_PROGRESS"
        ledger.finish("run-1", outcome="COMPLETED")
        # state 是"认领生命周期"（IN_PROGRESS / DONE），
        # outcome 才是业务结果（COMPLETED / FAILED:xxx）。两者不同维度。
        assert ledger.claim_state("run-1") == "DONE"
        assert ledger.outcome_for("run-1") == "COMPLETED"

    def test_finish_records_outcome(self):
        ledger = RunLedger()
        ledger.claim("run-1")
        ledger.finish("run-1", outcome="FAILED:BUDGET_EXHAUSTED")
        assert ledger.outcome_for("run-1") == "FAILED:BUDGET_EXHAUSTED"

    def test_unknown_run_has_no_claim_state(self):
        ledger = RunLedger()
        assert ledger.claim_state("run-x") is None


class TestClaimIsAtomicUnderConcurrency:
    def test_only_one_thread_wins(self):
        """真正的并发：N 个线程同时认领同一 run，必须只有一个成功。"""
        ledger = RunLedger()
        successes: list[bool] = []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()  # 让所有线程尽量同时进入
            successes.append(ledger.claim("run-race"))

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert sum(successes) == 1, f"exactly one claim must succeed, got {successes}"


class TestPersistenceAcrossRestart:
    def test_claim_survives_reopen(self, tmp_path):
        """认领必须持久化：worker 崩溃重启后不能重复调查同一 run。"""
        path = tmp_path / "ledger.db"
        ledger = RunLedger(path)
        assert ledger.claim("run-1") is True
        ledger.close() if hasattr(ledger, "close") else None

        reopened = RunLedger(path)
        assert reopened.claim("run-1") is False, (
            "a claimed run must stay claimed across process restart")

    def test_in_progress_run_can_be_reclaimed_after_crash(self, tmp_path):
        """崩溃恢复：IN_PROGRESS 且超时的认领应可被重新认领。

        否则 worker 崩在调查中途，这个 run 就永远卡在 IN_PROGRESS。
        """
        path = tmp_path / "ledger.db"
        ledger = RunLedger(path)
        ledger.claim("run-1")
        import time
        time.sleep(1.2)  # 让认领时间超过阈值
        assert ledger.reclaimable("run-1", older_than_seconds=1) is True


class TestWorkerUsesClaim:
    """端到端：worker 必须靠 claim 防重复，而不是"查一下再跑"。"""

    def test_two_workers_on_one_message_only_one_runs(self):
        """两个 worker 共享同一账本，同一条消息只应被调查一次。"""
        from traceops.runtime.run_ledger import RunLedger

        ledger = RunLedger()
        assert ledger.claim("run-001") is True
        # 第二个 worker 用同一账本
        assert ledger.claim("run-001") is False

    def test_claim_does_not_break_reprocessing_after_finish(self):
        """完成后的 run 依然不能被重新认领（这是幂等的定义）。"""
        ledger = RunLedger()
        ledger.claim("run-001")
        ledger.finish("run-001", outcome="COMPLETED")
        assert ledger.claim("run-001") is False


class TestSqliteCrossThreadClaim:
    """SQLite 连接默认绑定创建它的线程。

    单线程测试永远发现不了这个问题；一旦 worker 变成线程池模型，
    `claim` 会直接抛 `sqlite3.ProgrammingError`。用真实多线程压一遍。
    """

    def test_many_threads_claiming_distinct_runs(self, tmp_path):
        import threading

        path = tmp_path / "ledger.db"
        ledger = RunLedger(path)
        run_ids = [f"run-{i}" for i in range(5)]
        wins: list[str] = []
        lock = threading.Lock()

        def worker():
            mine = [rid for rid in run_ids if ledger.claim(rid)]
            with lock:
                wins.extend(mine)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(wins) == 5, f"each run must be claimed exactly once, got {wins}"
        assert set(wins) == set(run_ids), f"unexpected claim set: {wins}"
