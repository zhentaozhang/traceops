"""有限重试 + 指数退避 + 抖动，用于瞬时网络故障。

## 为什么需要（对照生产实践）

实测本项目的 `openai_client.py` 与 `tools/adapters.py` **没有任何重试**
（`retry|backoff|attempt` 计数为 0）。后果具体而不抽象：

- 数据源一次超时 → `collect` 记入 `missing_data` → 该数据源**整类证据缺失**
  → 覆盖率下降 → 语义闸门降级 → 结论不可靠
- LLM 端点一次 503 → `ModelUnavailable` → 模型路径直接放弃

这些都是一次重试就能恢复的**瞬时**故障。

## 三条硬约束（都来自踩过的坑）

1. **只重试瞬时错误**。4xx / 契约违约 / 解析失败重试也不会变好；
   重试它们只会把"明确的错误"变成"慢一点的错误"，并浪费预算。
2. **必须有上限**。无限重试是新的故障源 —— 对端真的挂了时，
   我们会一直占着线程和预算。
3. **退避要递增且带抖动**。立即重试会制造 thundering herd：
   所有 worker 在同一毫秒重试，把刚要恢复的对端再次打垮。

## 为什么不用第三方库

依赖策略是标准库 + pydantic + jsonschema。重试逻辑本身不到 100 行，
引入 `tenacity` 只为省这 50 行不划算，而且它的默认语义
（哪些异常可重试）与本项目"证据优先、明确失败"的原则不完全一致。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable

#: 视为"瞬时"的 HTTP 状态码。
#:
#: 429（限流）也在内：它不是永久错误，只是要求等一会儿再来。
#: 5xx 全部可重试 —— 它们是服务端暂时性问题。
#: 4xx（除 429）一律不重试：请求本身有问题，重复发送毫无意义。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def is_retryable_status(status: int) -> bool:
    """HTTP 状态码是否值得重试。"""
    return status in _RETRYABLE_STATUS


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略。

    | 字段 | 作用 | 为什么是这个默认值 |
    |---|---|---|
    | `max_attempts` | 含首次在内的总尝试次数 | 3 = 首次 + 2 次重试，足够扛住抖动又不放大故障 |
    | `base_delay` | 首次退避基数 | 0.5s：给对端一点恢复时间，又不至于让一次调查卡住 |
    | `max_delay` | 退避上限 | 8s：再长就不如直接判失败，让上层快速降级 |
    | `jitter` | 抖动比例 | 0.5 = ±50%，打散同时重试的 worker |
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.5

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must be non-negative")
        if not 0 <= self.jitter <= 1:
            raise ValueError(f"jitter must be in [0, 1], got {self.jitter}")

    def delay_for(self, attempt: int) -> float:
        """第 `attempt` 次失败后应等待多久（attempt 从 1 开始）。

        指数增长 → 截断到 max_delay → 加抖动。
        抖动让多个 worker 的重试时间**错开**，避免同时打在同一毫秒。
        """
        if attempt <= 0:
            return 0.0
        raw = self.base_delay * (2 ** (attempt - 1))
        capped = min(raw, self.max_delay)
        if self.jitter <= 0:
            return capped
        # 在 ±jitter 比例内随机：范围 (1-jitter, 1+jitter)
        factor = 1.0 + random.uniform(-self.jitter, self.jitter)
        return max(0.0, capped * factor)


#: 默认可重试的异常类型。
#:
#: 刻意**不包含** `ValueError` / 契约类异常：它们是确定性错误，
#: 重试只会重复同样的失败。需要更宽/更窄的调用方可以传 `retryable` 覆盖。
_DEFAULT_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)


def _default_retryable(exc: BaseException) -> bool:
    return isinstance(exc, _DEFAULT_RETRYABLE_EXCEPTIONS)


def with_retry(
    fn: Callable[[], Any],
    *,
    policy: RetryPolicy | None = None,
    retryable: Callable[[BaseException], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """执行 `fn`，失败时按策略重试；最终仍失败则抛出**原始异常**。

    Args:
        retryable: 覆盖默认判定。HTTP 调用方应传一个同时看异常与状态码的谓词。
        sleep: 可注入（测试不真的 waiting）。

    为什么最终抛原始异常而不是"重试耗尽"包装异常：
    调用方需要看到**真实原因**才能决定降级策略（是超时还是 4xx 完全不同）。
    包装一层会把这个信息埋掉。
    """
    effective = policy or RetryPolicy()
    decide = retryable or _default_retryable

    last_exc: BaseException | None = None
    for attempt in range(1, effective.max_attempts + 1):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 — 需捕获全部再判定
            last_exc = exc
            if not decide(exc):
                # 确定性错误：重试不会变好，立刻失败
                raise
            if attempt >= effective.max_attempts:
                break
            sleep(effective.delay_for(attempt))

    assert last_exc is not None
    raise last_exc
