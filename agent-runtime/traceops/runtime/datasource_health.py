"""数据源健康：把"这次缺一块"变成"它已经坏了一整天"。

## 缺陷

`missing_data` 只活在**单次调查**里。同一个 Loki 端点连续失败二十次，
表现就是二十份报告各自少一条日志证据，每份报告都"解释得通"：
数据源当时不可用。没有任何一处说得出"loki 从早上九点起就没成功过"。

后果很具体：abstention 的解释力被削弱。报告说 INCONCLUSIVE 是因为证据不足，
读者无法区分两种情况 ——

- 这个故障本身就没有日志（阴性证据，结论其实很硬）；
- 我们的日志查询根本发不出去（工具故障，结论不该被采信）。

这两种情况的修法完全相反，而单次调查里它们长得一模一样。

## 做法

一个跨调查的计数器：每个数据源记连续失败数、总失败/成功数、最后一次错误。
报告把它放进 `run_snapshot`（自由字段，不需要改契约），于是：

- 单次报告能说"loki 已连续失败 7 次"；
- 连续失败数 ≥ 3 的数据源会被明确点名（`persistently_failing`）。

计数放在进程内、由调用方显式传入（`cli/worker.py` 每个 run 复用同一个实例），
因为"跨调查"的边界就是"同一个 worker 进程"。不落盘：重启后归零是可接受的，
而落盘的代价（清理、并发写、schema 迁移）与收益不成比例。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: 连续失败多少次算"持续故障"。取 3 是因为两次失败很可能是抖动，
#: 三次连续失败在"几分钟一次"的调查节奏下已覆盖十分钟以上。
PERSISTENT_FAILURE_THRESHOLD = 3


@dataclass
class SourceHealth:
    """一个数据源的累计健康读数。"""

    source_id: str
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_error: str | None = None
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None
    #: 连续失败期间第一次失败的时间。用来回答"坏了多久"，
    #: 而不是只知道"坏了"。
    failing_since: datetime | None = None

    @property
    def persistently_failing(self) -> bool:
        return self.consecutive_failures >= PERSISTENT_FAILURE_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "last_error": self.last_error,
            "last_failure_at": (
                self.last_failure_at.isoformat() if self.last_failure_at else None),
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None),
            "failing_since": (
                self.failing_since.isoformat() if self.failing_since else None),
            "persistently_failing": self.persistently_failing,
        }


@dataclass
class DatasourceHealthTracker:
    """跨调查共享的数据源健康表。"""

    _sources: dict[str, SourceHealth] = field(default_factory=dict)

    def _entry(self, source_id: str) -> SourceHealth:
        entry = self._sources.get(source_id)
        if entry is None:
            entry = SourceHealth(source_id=source_id)
            self._sources[source_id] = entry
        return entry

    def record_success(self, source_id: str, *, at: datetime | None = None) -> None:
        """一次成功（**包括"查到了但没有数据"**）。"""
        now = at or datetime.now(UTC)
        entry = self._entry(source_id)
        entry.consecutive_failures = 0
        entry.total_successes += 1
        entry.last_success_at = now
        entry.failing_since = None

    def record_failure(self, source_id: str, error: str, *,
                       at: datetime | None = None) -> None:
        now = at or datetime.now(UTC)
        entry = self._entry(source_id)
        if entry.consecutive_failures == 0:
            entry.failing_since = now
        entry.consecutive_failures += 1
        entry.total_failures += 1
        entry.last_failure_at = now
        entry.last_error = str(error)[:200]

    def persistently_failing(self) -> list[str]:
        """连续失败超阈值的数据源（按连续失败数降序）。"""
        entries = [e for e in self._sources.values() if e.persistently_failing]
        entries.sort(key=lambda e: (-e.consecutive_failures, e.source_id))
        return [e.source_id for e in entries]

    def summary(self) -> list[dict[str, Any]]:
        """全部读数（有失败的排前面，便于报告一眼看到问题源）。"""
        entries = sorted(
            self._sources.values(),
            key=lambda e: (-e.consecutive_failures, -e.total_failures, e.source_id),
        )
        return [entry.to_dict() for entry in entries]

    def was_failing(self, source_id: str) -> bool:
        """这个数据源在这次调查之前是否已经处于持续故障状态。"""
        entry = self._sources.get(source_id)
        return bool(entry and entry.persistently_failing)
