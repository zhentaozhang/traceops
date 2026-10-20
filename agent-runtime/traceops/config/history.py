"""策略变更的历史与"这一轮用的哪套策略"的归属。

被替换的缺陷：策略旋钮（阈值、排序权重、预算）在实现里是模块常量，
改了以后**没有任何记录**。于是同一批报告可能横跨两套策略，而报告本身
看不出来 —— 事后比较"上周为什么更准"时，唯一能查的只有代码的 git 历史，
且无法知道哪次调查对应哪个版本。

三个刻意选择：

1. **只记变化，不记每次运行**。每次运行都写一条 = 30 条里 29 条是噪声，
   真正的拐点被埋掉。基线（第一次观察）只记指纹、不记变更。
2. **变更用点分路径表达**（`thresholds.error_rate`），不用自由文本：
   自由文本无法聚合，也无法回答"最近 30 次改动里最常被动的那个旋钮"。
3. **跨 revision 不可比**：旋钮集合本身变了（增删字段）时，差异列表
   没有意义 —— 宁可标记 `comparable=False`，也不要拿两套不同字段的
   差集去解释结论变化。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: 保留的变更条数上限。与 super-agent 的 `policyHistory` 同为 30：
#: 够覆盖一个迭代周期，又不至于让"最近发生了什么"需要翻页。
CHANGE_HISTORY_LIMIT = 30

#: 单条变更最多列多少个受影响字段。超过时截断并给出总数，
#: 避免一次"全量重设"把历史撑成几百行。
MAX_CHANGED_PATHS = 20


@dataclass(frozen=True)
class StrategyChange:
    """一次策略变更。`changed_paths` 为空且 `comparable=True` 表示指纹变了但字段没变
    （例如提示词片段内容变了 —— 那确实是策略变化，只是不在这批数值旋钮里）。"""

    previous_digest: str | None
    digest: str
    revision: str
    previous_revision: str | None
    changed_paths: tuple[str, ...]
    occurred_at: datetime
    #: 两次观察的旋钮集合不同（字段增删），差异列表不可直接解释结论变化。
    comparable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_digest": self.previous_digest,
            "digest": self.digest,
            "revision": self.revision,
            "previous_revision": self.previous_revision,
            "changed_paths": list(self.changed_paths),
            "occurred_at": self.occurred_at.isoformat(),
            "comparable": self.comparable,
        }


@dataclass
class StrategyHistory:
    """最近 N 次策略变更。进程内、有界，不需要持久化也能回答
    "这次调查用的是哪套策略、相对上次变了什么"。"""

    limit: int = CHANGE_HISTORY_LIMIT
    _changes: deque[StrategyChange] = field(default_factory=lambda: deque(maxlen=CHANGE_HISTORY_LIMIT))
    _last: tuple[str, dict[str, Any], str] | None = None

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("strategy history limit must be positive")
        if self._changes.maxlen != self.limit:
            self._changes = deque(self._changes, maxlen=self.limit)

    def observe(
        self,
        *,
        knobs: dict[str, Any],
        digest: str,
        revision: str,
        at: datetime | None = None,
    ) -> StrategyChange | None:
        """观察一次运行的策略。返回**本次观察发现的变更**，首次观察返回 None。

        首次观察只建立基线：把它记成"从空到有"的变更是噪声，
        会让第一批报告全都带上一条假的策略变更记录。
        """
        when = at or datetime.now(UTC)
        previous = self._last
        if previous is None:
            self._last = (digest, dict(knobs), revision)
            return None

        previous_digest, previous_knobs, previous_revision = previous
        self._last = (digest, dict(knobs), revision)
        if digest == previous_digest:
            return None

        comparable = previous_revision == revision
        changed = list(_diff(previous_knobs, knobs)) if comparable else []
        if len(changed) > MAX_CHANGED_PATHS:
            changed = changed[:MAX_CHANGED_PATHS]

        change = StrategyChange(
            previous_digest=previous_digest,
            digest=digest,
            revision=revision,
            previous_revision=previous_revision,
            changed_paths=tuple(changed),
            occurred_at=when,
            comparable=comparable,
        )
        self._changes.append(change)
        return change

    def changes(self, limit: int | None = None) -> list[StrategyChange]:
        """最近的变更，新的在前。"""
        items = list(self._changes)
        items.reverse()
        if limit is not None:
            return items[:limit]
        return items

    @property
    def current_digest(self) -> str | None:
        return self._last[0] if self._last is not None else None


def _diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """只在两边都有的字段上比较 —— 缺失的那个字段不是"变了"，是"这批观察里没有"。"""
    paths = []
    for path in sorted(set(before) & set(after)):
        if before[path] != after[path]:
            paths.append(path)
    return paths
