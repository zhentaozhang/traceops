"""证据保留策略（Retention）。

## 为什么必须有

证据会无限增长：每次调查 3~12 条，每个事故可能多轮。没有保留策略时
SQLite 文件只增不减，且历史事故的证据已无审计价值却仍参与查询。

## 为什么删除必须极其谨慎

"到点就删"是危险默认值，有三条硬约束：

1. **事故未关闭时绝不删**：正在处理的事故需要证据，即使超出保留期
   （保留期是"清理陈旧数据"的规则，不是"删掉正在用的数据"的许可）
2. **被关系引用的证据绝不删**：删掉一端，关系就指向不存在的目标，
   影响分析与传播链会变成悬空引用
3. **默认支持 dry-run**：先看清楚会删什么再动手，这是运维的基本礼貌

## 三个数字分开报告

purged（真删了）/ protected（被引用保护）/ retained（未到期）。
只报"删了几条"会让人误以为清理很彻底，实际可能是大量数据被保护着。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetentionPolicy:
    """保留策略。

    Args:
        retain_days: 超过这个天数的证据才考虑删除。0 表示"只看事故是否关闭"。
        require_closed_incident: 是否要求事故已终结才允许删除（默认 True）。
    """

    retain_days: int = 30
    require_closed_incident: bool = True

    def __post_init__(self) -> None:
        if self.retain_days < 0:
            raise ValueError(f"retain_days must be >= 0, got {self.retain_days!r}")


@dataclass
class RetentionResult:
    purged: int = 0
    protected: int = 0
    retained: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "purged": self.purged,
            "protected": self.protected,
            "retained": self.retained,
            "dry_run": self.dry_run,
        }
