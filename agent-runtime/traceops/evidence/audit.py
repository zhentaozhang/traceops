"""统一审计中心：把三处散落的审计合成一个可查询的时间线。

## 为什么要做（PRD 5.7 的「审计中心」写的是"散落各处"）

三份审计各自为政，**连时间表示都不一样**：

| 来源 | 时间字段 | 类型 | 记录什么 |
|---|---|---|---|
| `ToolGateway.audit_log()` | `timestamp` | `datetime` | 工具调用（含失败） |
| `RunLedger` | `recorded_at` | **ISO 字符串** | run 生命周期 |
| `DeadLetterRecord` | `occurred_at` | **ISO 字符串** | 坏消息 |

后果不是"不好看"，而是**回答不了真实问题**：

> "这次调查为什么失败？"

答案需要同时看：哪个工具调用失败了（gateway）、run 最终什么状态（ledger）、
有没有消息进了死信（deadletter）。三份数据时间类型不同，**无法按时间线合并**
—— 而那正是排查唯一有用的视图。

## 本模块的边界（诚实声明）

- **只做归一化与查询**，不改变三处的写入方式
- **内存实现**：`ToolGateway._audit` 本来就是内存列表，进程重启即清空。
  统一后仍然如此 —— 这个局限必须明说，不能因为"有了审计中心"就以为有了持久化
- 不做权限/多租户（PRD 里那是 P2 的另一个项）

## 设计取舍：为什么用 `kind` 而不是三个独立子类型

审计事件的本体是**同一组问题**：什么时候、谁、发生了什么、结果如何。
用 `kind` 区分来源，让时间线可以跨源排序；如果做成三个子类，
调用方每次都要自己 instanceof 分派 —— 那不如不统一。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Iterable


class AuditKind(str, Enum):
    TOOL_CALL = "tool_call"
    RUN_OUTCOME = "run_outcome"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True)
class AuditEvent:
    """一条归一化后的审计事件。

    frozen 是硬要求：**事后修改审计等于销毁证据**。
    """

    kind: AuditKind
    occurred_at: datetime
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "occurred_at": self.occurred_at.isoformat(),
            "subject": self.subject,
            "detail": dict(self.detail),
        }


def _parse_time(value: Any) -> datetime:
    """统一时间表示。

    gateway 给 `datetime`，账本/死信给 ISO 字符串。naive datetime 一律按 UTC
    处理 —— 本项目所有时间都是 UTC，把 naive 当本地时间会静默偏移数小时，
    那会让时间线看起来"因果颠倒"。
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise TypeError(f"cannot parse timestamp: {value!r}")


class AuditCenter:
    """内存审计中心：归一化 + 查询。"""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    # ---- 归一化入口：每类来源一个适配器 ----
    @staticmethod
    def from_gateway_records(records: Iterable[dict[str, Any]]) -> list[AuditEvent]:
        events: list[AuditEvent] = []
        for record in records:
            events.append(AuditEvent(
                kind=AuditKind.TOOL_CALL,
                occurred_at=_parse_time(record.get("timestamp")),
                subject=str(record.get("tool") or "unknown-tool"),
                detail={
                    "outcome": record.get("outcome"),
                    "params": record.get("params") or {},
                    "error": record.get("error"),
                    "duration_ms": record.get("duration_ms", 0),
                },
            ))
        return events

    @staticmethod
    def from_ledger_rows(rows: Iterable[dict[str, Any]]) -> list[AuditEvent]:
        events: list[AuditEvent] = []
        for row in rows:
            events.append(AuditEvent(
                kind=AuditKind.RUN_OUTCOME,
                occurred_at=_parse_time(row.get("recorded_at")),
                subject=str(row.get("run_id") or "unknown-run"),
                detail={
                    "outcome": row.get("outcome"),
                    "state": row.get("state"),
                },
            ))
        return events

    @staticmethod
    def from_dead_letter(record: Any) -> AuditEvent:
        """`DeadLetterRecord`（dataclass 或 dict）→ 审计事件。

        同时接受两种形状：调用方可能从 `to_dict()` 拿到 dict，
        也可能直接传对象。宁可宽容一点，也不要让调用方写分支。
        """
        def _get(name: str) -> Any:
            if isinstance(record, dict):
                return record.get(name)
            return getattr(record, name, None)

        category = _get("category")
        category_value = getattr(category, "value", category)
        return AuditEvent(
            kind=AuditKind.DEAD_LETTER,
            occurred_at=_parse_time(_get("occurred_at")),
            subject=str(_get("stream") or "unknown-stream"),
            detail={
                "entry_id": _get("original_entry_id"),
                "group": _get("group"),
                "consumer": _get("consumer"),
                "category": category_value,
                "reason": _get("reason"),
            },
        )

    # ---- 查询 ----
    def add(self, event: AuditEvent) -> None:
        self._events.append(event)

    def add_all(self, events: Iterable[AuditEvent]) -> None:
        self._events.extend(events)

    def timeline(
        self,
        *,
        kind: AuditKind | None = None,
        subject: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AuditEvent]:
        """按时间正序返回事件，可过滤。

        排序在**查询时**做而不是插入时：调用方可以随意分批 add，
        时间线仍然正确。代价是每次查询排序，对审计规模（数千条）无影响。
        """
        selected = self._events
        if kind is not None:
            selected = [e for e in selected if e.kind is kind]
        if subject is not None:
            selected = [e for e in selected if e.subject == subject]
        if since is not None:
            since_aware = since if since.tzinfo else since.replace(tzinfo=UTC)
            selected = [e for e in selected if e.occurred_at >= since_aware]
        if until is not None:
            until_aware = until if until.tzinfo else until.replace(tzinfo=UTC)
            selected = [e for e in selected if e.occurred_at <= until_aware]
        return sorted(selected, key=lambda e: e.occurred_at)

    def count(self) -> int:
        return len(self._events)
