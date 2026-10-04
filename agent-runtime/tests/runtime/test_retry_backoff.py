"""瞬时故障必须有**有限重试**，否则一次网络抖动就丢一个数据源。

## 缺陷（对照生产实践发现的）

实测：`runtime/openai_client.py` 与 `tools/adapters.py` 里
**没有任何重试或退避**（grep `retry|backoff|attempt` 计数为 0）。

后果具体而不抽象：

- 数据源（Prometheus/Loki/Tempo）一次超时 → `collect` 记入 `missing_data`
  → 该数据源整类证据缺失 → 证据覆盖率下降 → 语义闸门降级 → 结论不可靠
- LLM 端点一次 503 → `ModelUnavailable` → 模型路径直接放弃

这些都是**瞬时**故障，重试一两次就能恢复。检索到的生产实践一致指出：
重试要用**指数退避 + 抖动**，并且**只重试可重试的错误**
（连接/超时/5xx），不能重试 4xx 或契约违约 —— 后者重试也不会变好。

## 三条必须成立的约束

1. **只重试瞬时错误**：4xx / 契约违约 / 解析失败**不重试**
2. **有上限**：不能无限重试（会变成新的故障源）
3. **退避递增**：立即重试只会加重对端压力（thundering herd）
"""
from __future__ import annotations

import pytest

from traceops.runtime.retry_http import RetryPolicy, is_retryable_status, with_retry


class TestPolicyShape:
    def test_default_policy_has_bounded_attempts(self):
        policy = RetryPolicy()
        assert policy.max_attempts >= 2, "one retry is the minimum useful case"
        assert policy.max_attempts <= 5, "more than 5 attempts usually amplifies the outage"

    def test_backoff_grows(self):
        """退避要递增 —— 但抖动会让相邻值重叠，所以比较**基准值**（jitter=0）。

        这是我自己写错过的测试：带抖动时 delay_for(1) 可能偶尔大于
        delay_for(2)（两者都在各自区间内随机）。递增性属于**基准曲线**，
        抖动是在其上叠加的扰动，不该用带抖动的采样验证单调性。
        """
        no_jitter = RetryPolicy(base_delay=0.01, max_delay=1.0, jitter=0.0)
        assert no_jitter.delay_for(1) < no_jitter.delay_for(2) < no_jitter.delay_for(3)

        # 带抖动时仍能验证期望值单调（多次采样取均值）
        with_jitter = RetryPolicy(base_delay=0.01, max_delay=1.0, jitter=0.5)
        means = [sum(with_jitter.delay_for(n) for _ in range(50)) / 50 for n in (1, 2, 3)]
        assert means[0] < means[1] < means[2], f"expected growth, got {means}"

    def test_backoff_is_capped(self):
        """上限约束的是**指数增长部分**，抖动会在此基础上小幅浮动。

        不加抖动时精确等于上限；加抖动时允许超出 jitter 比例 ——
        这正是抖动的意义（避免同步重试），所以断言用上界而非等号。
        """
        no_jitter = RetryPolicy(base_delay=0.01, max_delay=0.5, jitter=0.0)
        assert no_jitter.delay_for(10) == 0.5

        with_jitter = RetryPolicy(base_delay=0.01, max_delay=0.5, jitter=0.5)
        # jitter=0.5 时上限为 max_delay * 1.5
        assert with_jitter.delay_for(10) <= 0.5 * 1.5

    def test_backoff_has_jitter_room(self):
        """抖动是必需的：没有抖动的重试会同步打在同一时刻。"""
        policy = RetryPolicy(base_delay=0.01, max_delay=1.0, jitter=0.5)
        values = {round(policy.delay_for(3), 6) for _ in range(20)}
        assert len(values) > 1, "jitter must vary the delay, got constant"
        assert all(0 <= v <= policy.max_delay for v in values)


class TestRetryableClassification:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_transient_status_is_retryable(self, status):
        assert is_retryable_status(status)

    @pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 422])
    def test_non_transient_status_is_not_retryable(self, status):
        assert not is_retryable_status(status)


class TestWithRetry:
    def test_succeeds_on_first_try_without_delay(self):
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        assert with_retry(fn, policy=RetryPolicy(max_attempts=3)) == "ok"
        assert len(calls) == 1

    def test_retries_transient_failure_then_succeeds(self):
        state = {"n": 0}

        def fn():
            state["n"] += 1
            if state["n"] < 3:
                raise ConnectionError("transient")
            return "ok"

        result = with_retry(fn, policy=RetryPolicy(max_attempts=3, base_delay=0.001))
        assert result == "ok"
        assert state["n"] == 3

    def test_gives_up_after_max_attempts(self):
        def fn():
            raise ConnectionError("always down")

        with pytest.raises(ConnectionError):
            with_retry(fn, policy=RetryPolicy(max_attempts=3, base_delay=0.001))

    def test_does_not_retry_non_transient_error(self):
        """4xx / 契约违约重试也不会变好，必须立刻失败。"""
        calls = []

        def fn():
            calls.append(1)
            raise ValueError("bad request: not retryable")

        with pytest.raises(ValueError):
            with_retry(fn, policy=RetryPolicy(max_attempts=3, base_delay=0.001))
        assert len(calls) == 1, "a deterministic error must not be retried"

    def test_retryable_predicate_can_be_overridden(self):
        """不同调用方对"可重试"的定义不同（HTTP 状态 vs 异常类型）。"""
        calls = []

        def fn():
            calls.append(1)
            raise RuntimeError("custom")

        with pytest.raises(RuntimeError):
            with_retry(fn, policy=RetryPolicy(max_attempts=2, base_delay=0.001),
                       retryable=lambda exc: False)
        assert len(calls) == 1
