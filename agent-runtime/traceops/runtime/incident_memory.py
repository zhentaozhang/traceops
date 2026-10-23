"""跨事故记忆：同一个服务以同一种方式坏过几次。

## 为什么需要它

一次调查只看得到自己那个窗口。于是"order-service 每个月都因为连接池耗尽
挂一次"这件事，对每一次调查来说都是全新的 —— 每次都要从零推导，
而且每次的结论都可能不同（同样的证据、同样的规则，排序却靠 UUID 兜底）。

人不是这样工作的：值班同学看到 order-service 报警，第一反应是
"又是那个连接池"。这条经验值钱，但它目前只存在于人的脑子里。

## 铁律：记忆只能调权，不能单独支撑结论

记忆是**先验**，不是证据。它能让两个候选的排序翻转，但它绝不能
让一个没有任何观测的候选变成 `SUPPORTED` —— `ranking._evaluate` 的
状态分类要求 `supporting_evidence_count > 0` 才可能 SUPPORTED，
而记忆不产生 link、不产生 Evidence。这条约束是结构性的，不是靠自觉。

同理，`refuted` 也记：一个候选被反复推翻，"它又来了"不该再加分。
所以报上去的是净值（`hits - refuted`），且只有净值为正才算"以前见过"。

## 为什么用 SQLite 而不是内存字典

记忆的价值在于**跨进程重启**。内存字典在 worker 重启后就没了，
而"每个月挂一次"这种模式恰好跨越很多次重启。测试里可以传 `None`
走内存库，生产应当给一个路径。
"""
from __future__ import annotations

import sqlite3

from traceops.runtime.sqlite_config import configure_connection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MEMORY_LIMIT = 5000
"""表的上限。记忆是辅助信号，不值得为它无界增长 ——
超过上限时淘汰最久没出现的那批。"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    service TEXT NOT NULL,
    fault_type TEXT NOT NULL,
    component TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 0,
    refuted INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (service, fault_type, component)
)
"""


@dataclass(frozen=True)
class Prior:
    """某个 (服务, 机制, 组件) 的历史计数。"""

    service: str
    fault_type: str
    component: str
    hits: int
    refuted: int
    last_seen: datetime

    @property
    def net(self) -> int:
        return self.hits - self.refuted

    @property
    def corroborated(self) -> bool:
        """以前确实这样坏过（而不是以前被推翻过）。"""
        return self.net > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "service": self.service,
            "fault_type": self.fault_type,
            "component": self.component or None,
            "hits": self.hits,
            "refuted": self.refuted,
            "last_seen": self.last_seen.isoformat(),
        }


class IncidentMemory:
    """SQLite 支撑的事故记忆。线程内使用；多进程写同一文件靠 SQLite 锁。"""

    def __init__(self, path: str | Path | None = None, *, limit: int = MEMORY_LIMIT) -> None:
        if limit < 1:
            raise ValueError(f"memory limit must be positive, got {limit}")
        self._limit = limit
        self._path = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        # 第四处 SQLite。sqlite_config 的 docstring 写过"新增第四个库时必然漏"，
        # 这里就是那次遗漏：默认 pragma 下读阻塞写、拿不到锁立刻抛
        # `database is locked`，而记忆的写入发生在每次调查结束 —— 正好与
        # 下一次调查的读取重叠。
        self._conn = configure_connection(
            sqlite3.connect(str(self._path) if self._path else ":memory:",
                            check_same_thread=False)
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    # ---------- 读 ----------

    def recall(self, *, service: str, fault_type: str,
               component: str | None = None) -> Prior | None:
        """查历史。`component=None` 时把该服务该机制的所有组件合计。

        为什么要合计：候选里"有具体组件"和不带组件是两种粒度，
        而历史里可能只记着 "order-service / pool_exhaustion / redis-connection-pool"。
        查 "order-service / pool_exhaustion"（不带组件）时应该想得起来它。
        """
        if not service or not fault_type:
            return None
        if component:
            row = self._conn.execute(
                "SELECT service, fault_type, component, hits, refuted, last_seen "
                "FROM memories WHERE service = ? AND fault_type = ? AND component = ?",
                (service, fault_type, component),
            ).fetchone()
            if row is None:
                return None
            return _to_prior(row)

        row = self._conn.execute(
            "SELECT service, fault_type, '' AS component, "
            "SUM(hits) AS hits, SUM(refuted) AS refuted, MAX(last_seen) AS last_seen "
            "FROM memories WHERE service = ? AND fault_type = ?",
            (service, fault_type),
        ).fetchone()
        if row is None or row["hits"] is None:
            return None
        return _to_prior(row)

    def summary(self, limit: int = 10) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT service, fault_type, component, hits, refuted, last_seen "
            "FROM memories ORDER BY (hits - refuted) DESC, last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_to_prior(row).to_dict() for row in rows]

    # ---------- 写 ----------

    def record(self, *, service: str, fault_type: str, component: str | None = None,
               refuted: bool = False, at: datetime | None = None) -> Prior:
        """记一次结局。`refuted=True` 记的是"这个候选被推翻了"。"""
        if not service or not fault_type:
            raise ValueError("memory record needs both service and fault_type")
        moment = at or datetime.now(UTC)
        key = (service, fault_type, component or "")
        column = "refuted" if refuted else "hits"
        self._conn.execute(
            f"""
            INSERT INTO memories (service, fault_type, component, hits, refuted, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(service, fault_type, component) DO UPDATE SET
                {column} = {column} + 1,
                last_seen = excluded.last_seen
            """,
            (key[0], key[1], key[2], 0 if refuted else 1, 1 if refuted else 0,
             moment.isoformat()),
        )
        self._evict_if_needed()
        self._conn.commit()
        prior = self.recall(service=service, fault_type=fault_type, component=component)
        assert prior is not None  # 刚写进去的行必须能读回来
        return prior

    def forget(self, *, service: str, fault_type: str, component: str | None = None) -> None:
        self._conn.execute(
            "DELETE FROM memories WHERE service = ? AND fault_type = ? AND component = ?",
            (service, fault_type, component or ""),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------- 内部 ----------

    def _count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        return int(row["n"])

    def _evict_if_needed(self) -> None:
        overflow = self._count() - self._limit
        if overflow <= 0:
            return
        self._conn.execute(
            "DELETE FROM memories WHERE rowid IN "
            "(SELECT rowid FROM memories ORDER BY last_seen ASC LIMIT ?)",
            (overflow,),
        )


def _to_prior(row: sqlite3.Row) -> Prior:
    return Prior(
        service=row["service"],
        fault_type=row["fault_type"],
        component=row["component"] or "",
        hits=int(row["hits"] or 0),
        refuted=int(row["refuted"] or 0),
        last_seen=datetime.fromisoformat(row["last_seen"]),
    )
