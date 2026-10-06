"""死信流（Dead Letter）实现。

坏消息既不能 ACK 掉（数据静默消失、无法审计），也不能永远滞留 PEL
（占满 pending、反复被认领浪费时间）。

正确做法：转移到独立死信流 + 附可聚合的失败原因，再从主流 ACK。

分类必须用枚举而不是自由文本 —— 与 contracts 里的 failure_reason 同构，
否则"哪类坏消息最多"这个最基本的问题都答不了。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol


class FailureCategory(str, Enum):
    MALFORMED_JSON = "MALFORMED_JSON"
    MISSING_ENVELOPE = "MISSING_ENVELOPE"
    INVALID_REQUEST = "INVALID_REQUEST"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DeadLetterRecord:
    """一条被隔离的坏消息。

    `raw` 必须截断：坏消息常常是无界字符串（甚至是恶意构造的超大 payload），
    原样保存会拖垮死信流本身。
    """

    MAX_RAW_CHARS = 4000

    original_entry_id: str
    stream: str
    group: str
    consumer: str
    category: FailureCategory
    reason: str
    raw: str
    occurred_at: str
    truncated: bool = field(default=False)

    TRUNCATION_MARKER = "...[truncated]"

    def __post_init__(self) -> None:
        if len(self.raw) > self.MAX_RAW_CHARS:
            # 截断后**加上标记的全长**也必须 <= MAX_RAW_CHARS，
            # 否则"上限"只是看起来成立，实际仍会超界
            keep = self.MAX_RAW_CHARS - len(self.TRUNCATION_MARKER)
            object.__setattr__(self, "raw", self.raw[:keep] + self.TRUNCATION_MARKER)
            object.__setattr__(self, "truncated", True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_entry_id": self.original_entry_id,
            "stream": self.stream,
            "group": self.group,
            "consumer": self.consumer,
            "category": self.category.value,
            "reason": self.reason,
            "raw": self.raw,
            "occurred_at": self.occurred_at,
            "truncated": self.truncated,
        }


class DeadLetterSink(Protocol):
    """死信投递端口。生产实现写独立 Redis Stream（如 traceops.deadletter）。"""

    def send(self, record: DeadLetterRecord) -> None: ...


class InMemoryDeadLetterSink:
    """有界死信收件箱。

    为什么必须有界：`records` 原本只追加、无上限，而 `DeadLetterRecord`
    只限制了**单条原文长度**（MAX_RAW_CHARS），不限制**条数**。
    死信恰恰在故障期增长最快 —— 上游开始发畸形消息时每条都进死信。
    如果诊断工具本身把进程撑死，运维连"看看死信里有什么"都做不到。

    策略：保留**最近** N 条（排障要的是"刚发生了什么"，不是最早的），
    丢弃量单独计数（静默丢弃不可接受），并允许显式设 0 表示无界。
    """

    #: 默认上限。500 足够容纳一次故障风暴的样本用于排障，
    #: 又远小于"吃掉内存"的量级。
    DEFAULT_MAX_RECORDS = 500

    def __init__(self, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        if max_records < 0:
            raise ValueError(f"max_records must be >= 0, got {max_records!r}")
        #: 0 表示无界（显式选择，不是默认）。
        self.max_records = max_records
        self.records: list[DeadLetterRecord] = []
        #: 因超出上限被丢弃的条数。必须可读 —— 否则"有多少被丢了"无从判断。
        self.dropped = 0

    def send(self, record: DeadLetterRecord) -> None:
        self.records.append(record)
        if self.max_records and len(self.records) > self.max_records:
            # 保留尾部（最新的）：排障关心的是最近发生了什么
            overflow = len(self.records) - self.max_records
            del self.records[:overflow]
            self.dropped += overflow

    def count_by_category(self) -> dict[FailureCategory, int]:
        counts: dict[FailureCategory, int] = {}
        for record in self.records:
            counts[record.category] = counts.get(record.category, 0) + 1
        return counts


def classify_failure(error: str) -> FailureCategory:
    """把异常文本映射到可聚合类别。

    这里用文本匹配而不是异常类型，因为派生失败可能来自
    pydantic（ValidationError）、json（JSONDecodeError）、或纯 ValueError，
    统一用 message 匹配更稳，且便于测试。
    """
    lowered = (error or "").lower()
    if "expecting property name" in lowered or "expecting value" in lowered \
            or "jsondecodeerror" in lowered or "extra data" in lowered:
        return FailureCategory.MALFORMED_JSON
    if "envelope" in lowered:
        return FailureCategory.MISSING_ENVELOPE
    if "missing required fields" in lowered or "payload" in lowered \
            or "required property" in lowered:
        return FailureCategory.INVALID_REQUEST
    if "schema" in lowered or "validationerror" in lowered:
        return FailureCategory.SCHEMA_VIOLATION
    return FailureCategory.UNKNOWN


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
