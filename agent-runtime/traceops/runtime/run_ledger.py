"""Run 账本：幂等必须跨进程重启有效。

## 为什么不能只用内存 set

`InvestigationWorker._known_run_ids` 是进程内集合。进程重启后清空，
于是 Redis 里未 ACK 的消息、或上游重发的消息会被当成新消息，**同一个 run 被重复调查**。

重复调查的代价不只是钱：
- 两次可能查到不同时刻的数据，给出不同结论，两条都流向 Control Plane
- Control Plane 虽有 event_id 去重，但 Python 侧已经白跑一遍（含真实查询与模型调用）

## 为什么不直接用 EventStore 的表

职责不同。账本回答"这个 run 处理过没、结果是什么"，生命周期与证据无关；
证据可能因保留策略被清理，但"run X 已经处理过"必须一直有效，否则重跑。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from traceops.runtime.deadletter import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_ledger (
    run_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'DONE',
    claimed_at TEXT
);
"""


class RunLedger:
    """已处理 Run 的账本。

    传 `path` 时用 SQLite 持久化（跨重启有效）；不传时纯内存。
    接口在两种模式下一致。
    """

    def __init__(self, path: str | Path | None = None, *, preload: bool = True) -> None:
        self._path = Path(path) if path is not None else None
        self._conn: sqlite3.Connection | None = None
        self._records: dict[str, str] = {}
        # 认领必须是原子的：is_seen 与 record 之间隔着一整个调查时长，
        # 期间另一个 worker 也能通过检查 → 重复调查。用锁把"检查+占用"合并成一步。
        self._lock = threading.Lock()

        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False：认领逻辑本身用 threading.Lock 串行化，
            # 因此连接跨线程使用是安全的。默认 True 会让"线程池化的 worker"
            # 在 claim 时直接抛 ProgrammingError —— 单线程测试永远发现不了。
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            if preload:
                # 启动时预载：后续 is_seen 判断不必每次打库。
                # preload=False 用于"我已知这条处理过、想从干净状态开始"的场景。
                rows = self._conn.execute("SELECT run_id, outcome FROM run_ledger").fetchall()
                self._records = {run_id: outcome for run_id, outcome in rows}

    # ---- 查询 ----
    def is_seen(self, run_id: str) -> bool:
        if run_id in self._records:
            return True
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM run_ledger WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row is not None

    def outcome_for(self, run_id: str) -> str | None:
        if run_id in self._records:
            return self._records[run_id]
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT outcome FROM run_ledger WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        self._records[run_id] = row[0]
        return row[0]

    def count(self) -> int:
        if self._conn is None:
            return len(self._records)
        return self._conn.execute("SELECT COUNT(*) FROM run_ledger").fetchone()[0]

    # ---- 写入 ----
    # ---- 认领语义（并发安全） ----
    def claim(self, run_id: str) -> bool:
        """原子地认领一个 run。返回 True 表示由本调用方执行。

        这是修复重复调查的关键：`is_seen` + `record` 是两条独立操作，
        中间隔着整个调查时长。并发 worker（Redis XAUTOCLAIM 抢回 pending
        消息时就会发生）会同时通过检查，然后各自跑一遍。

        实现：内存模式下用锁 + 字典；SQLite 模式下用主键冲突
        （INSERT OR IGNORE 的影响行数就是"谁抢到了"的答案）。
        """
        with self._lock:
            if self._conn is not None:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO run_ledger "
                    "(run_id, outcome, recorded_at, state, claimed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, "IN_PROGRESS", now_iso(), "IN_PROGRESS", now_iso()),
                )
                self._conn.commit()
                claimed = cursor.rowcount == 1
                if claimed:
                    self._records[run_id] = "IN_PROGRESS"
                return claimed
            if run_id in self._records:
                return False
            self._records[run_id] = "IN_PROGRESS"
            return True

    def claim_state(self, run_id: str) -> str | None:
        """认领状态：IN_PROGRESS / DONE / None（从未见过）。

        区分两者很重要：IN_PROGRESS 说明"别人正在跑"（可能要等或抢回），
        DONE 说明"已经完成"（跳过即可）。
        """
        if self._conn is not None:
            row = self._conn.execute(
                "SELECT state FROM run_ledger WHERE run_id = ?", (run_id,)).fetchone()
            return row[0] if row else None
        outcome = self._records.get(run_id)
        if outcome is None:
            return None
        return "IN_PROGRESS" if outcome == "IN_PROGRESS" else "DONE"

    def finish(self, run_id: str, *, outcome: str) -> None:
        """把认领标记为完成（写最终 outcome）。"""
        self._records[run_id] = outcome
        if self._conn is not None:
            self._conn.execute(
                "UPDATE run_ledger SET outcome = ?, state = 'DONE', recorded_at = ? "
                "WHERE run_id = ?", (outcome, now_iso(), run_id))
            self._conn.commit()

    def reclaimable(self, run_id: str, *, older_than_seconds: float) -> bool:
        """崩溃恢复：处于 IN_PROGRESS 且认领时间超过阈值的 run 可被重新认领。

        没有这条的话，worker 崩在调查中途，这个 run 会永远卡在 IN_PROGRESS。
        """
        state = self.claim_state(run_id)
        if state != "IN_PROGRESS":
            return False
        if self._conn is None:
            # 内存模式无法表达时间，保守返回 True（进程重启本就清空内存）
            return True
        row = self._conn.execute(
            "SELECT claimed_at FROM run_ledger WHERE run_id = ?", (run_id,)).fetchone()
        if not row or not row[0]:
            return True
        from datetime import UTC, datetime

        try:
            claimed = datetime.fromisoformat(row[0])
        except ValueError:
            return True
        age = (datetime.now(UTC) - claimed).total_seconds()
        return age >= older_than_seconds

    def record(self, run_id: str, *, outcome: str) -> None:
        """记录一次处理结果。

        同一 run 重复记录按覆盖处理：一次失败后重试成功，最终结果应是成功，
        不能把"曾经失败"永久写在这里影响后续幂等判断。
        """
        if not run_id or not run_id.strip():
            raise ValueError("run_id is required")
        self._records[run_id] = outcome
        if self._conn is not None:
            self._conn.execute(
                """
                INSERT INTO run_ledger (run_id, outcome, recorded_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    outcome=excluded.outcome, recorded_at=excluded.recorded_at
                """,
                (run_id, outcome, now_iso()),
            )
            self._conn.commit()
