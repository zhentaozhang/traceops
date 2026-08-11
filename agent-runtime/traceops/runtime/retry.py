"""调查重试策略（蓝图 §8.4 / §13.2）。

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

from dataclasses import dataclass, field

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
    """只有明确列为可重试的原因才重试；其余（含未知）一律不重试。"""
    return (reason or "").strip().upper() in RETRYABLE_REASONS


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    reason: str
    delay_seconds: float = 0.0


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    deadline_seconds: float | None = None

    def should_attempt(self, *, attempt: int, reason: str,
                       elapsed_seconds: float = 0.0) -> bool:
        if not is_retryable(reason):
            return False
        if attempt >= self.max_attempts:
            return False
        if self.deadline_seconds is not None and elapsed_seconds >= self.deadline_seconds:
            return False
        return True

    def delay_for(self, attempt: int) -> float:
        """指数退避，带上限。没有上限时第 10 次要等 512 秒，等于放弃。"""
        raw = self.base_delay_seconds * (2 ** max(0, attempt))
        return min(raw, self.max_delay_seconds)


@dataclass
class RetryTracker:
    """一次 Run 的重试记账。进程内对象；持久化走 snapshot()。"""

    policy: RetryPolicy = field(default_factory=RetryPolicy)
    failures: int = 0
    last_reason: str | None = None
    succeeded: bool = False

    def record_failure(self, *, reason: str) -> None:
        self.failures += 1
        self.last_reason = reason

    def record_success(self) -> None:
        self.succeeded = True

    def decide(self, *, reason: str, elapsed_seconds: float = 0.0) -> RetryDecision:
        if self.succeeded:
            return RetryDecision(False, "already succeeded")

        if not is_retryable(reason):
            return RetryDecision(False, f"not retryable: {reason}")

        if not self.policy.should_attempt(
                attempt=self.failures, reason=reason, elapsed_seconds=elapsed_seconds):
            if self.policy.deadline_seconds is not None \
                    and elapsed_seconds >= self.policy.deadline_seconds:
                return RetryDecision(False, "deadline exceeded")
            return RetryDecision(
                False, f"attempts exhausted ({self.failures}/{self.policy.max_attempts})")

        return RetryDecision(
            True,
            f"retrying after {reason}",
            delay_seconds=self.policy.delay_for(self.failures),
        )

    def snapshot(self) -> dict:
        return {
            "failures": self.failures,
            "max_attempts": self.policy.max_attempts,
            "last_reason": self.last_reason,
            "succeeded": self.succeeded,
        }
