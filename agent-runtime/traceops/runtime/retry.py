"""调查失败原因的重试分类（蓝图 §8.4 / §13.2）。

## 这里只做分类，不做排期

"这次失败重试有没有意义"是 agent 侧的事：只有 agent 知道失败发生在
哪一步、是不是瞬时的。但"重试几次、隔多久重试、什么时候放弃"是**控制面**
的事：事故状态机和持久化 outbox 都在那边，而 agent 进程重启就丢了自己
那份计数。

所以本模块只剩分类器 `is_retryable`。重试的次数、退避和预算上限在
`control-plane/.../investigation/InvestigationRetryService.java` 里。

## 分类的依据

重试的价值在于"这次失败可能是瞬时的"。按这个标准把 contracts 的
`failure_reason` 枚举分成两类：

| 可重试 | 原因 |
|---|---|
| MCP_SERVER_UNAVAILABLE / MODEL_TIMEOUT / MODEL_UNAVAILABLE | 外部依赖瞬时不可用 |
| DATASOURCE_QUERY_ERROR | 查询瞬时失败（数据源存在但这次没查成） |
| CHECKPOINT_RESTORE_FAILED / INTERNAL_ERROR | 可能由竞态或临时资源问题引起 |

| 不可重试 | 原因 |
|---|---|
| BUDGET_EXHAUSTED / DEADLINE_EXCEEDED | 同样的预算/时限再跑必然再次失败，重试纯烧钱 |
| INVALID_REQUEST | 请求本身非法，重试不会让它变合法 |
| DATASOURCE_MISSING | 数据源不存在，不是瞬时问题 |
| CANCELLED_BY_USER | 用户主动取消，系统不该"好心"重试 |
| 未知原因（默认） | 宁可停下等人，也不要盲目循环 |

后两类是关键：**默认不重试更安全**。盲目重试会把一个需要人介入的问题
变成无限循环，同时烧掉预算。
"""
from __future__ import annotations

# contracts/events/v1/investigation-failed.schema.json 里的枚举，逐字对应
RETRYABLE_REASONS: frozenset[str] = frozenset({
    "MCP_SERVER_UNAVAILABLE",
    "MODEL_TIMEOUT",
    "MODEL_UNAVAILABLE",
    "DATASOURCE_QUERY_ERROR",
    "EVIDENCE_WRITE_ERROR",
    "CHECKPOINT_RESTORE_FAILED",
    "INTERNAL_ERROR",
})

NON_RETRYABLE_REASONS: frozenset[str] = frozenset({
    "BUDGET_EXHAUSTED",
    "DEADLINE_EXCEEDED",
    "INVALID_REQUEST",
    "DATASOURCE_MISSING",
    "CANCELLED_BY_USER",
})


def is_retryable(reason: str) -> bool:
    """只有明确列为可重试的原因才重试；其余（含未知）一律不重试。

    不可重试清单**先判**：两个清单是同一个枚举的互补划分，写在一起时
    一眼能看出它们没有重叠 —— 一旦有人往可重试清单里加了一个已经在
    不可重试清单里的值，这里会明确地按"不可重试"处理，而不是碰运气。
    """
    key = (reason or "").strip().upper()
    if key in NON_RETRYABLE_REASONS:
        return False
    return key in RETRYABLE_REASONS
