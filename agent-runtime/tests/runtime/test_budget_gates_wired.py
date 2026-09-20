"""预算的轮数/截止时间闸门必须真的接线，否则是虚设的。

## 缺陷（由 aurora/holmesgpt 深挖时指出，实测确认）

`BudgetManager` 声明了三道闸门：

```python
max_tool_calls   # 工具调用数
max_rounds       # 轮数
deadline_seconds # 墙钟时间
```

但 `begin_round()` —— 唯一检查 round 与 deadline 的方法 —— **全仓零调用方**。
后果：

1. `_rounds` 永远为 0，而 `worker.py:370` 把它当成真实用量上报
   （`run.record_usage(rounds=ctx.budget.rounds)`），事件里的 `rounds: 0` 是假数据
2. `deadline_seconds` 只在 `begin_round` 里检查 → **截止时间闸门从未生效**
3. `max_rounds` 同理 → **轮数上限从未生效**

这是"防御机制看起来存在、实际上不存在"的典型：代码里有、测试里没测、
生产里不生效。比没有更危险，因为它给人虚假的安全感。

## 为什么现在必须在 P1 之前修

P0 的 7 节点图是确定性单趟执行，确实只有"一轮"，所以 `rounds=0` 无害。
但 P1 要接入 LLM 的多轮 ReAct 循环 —— 一旦有真循环，
**轮数上限和截止时间就是防止无限烧钱的两道闸门**。
带着"闸门是虚的"进入 P1，等于没有闸门。

## 正确行为

- 一次调查至少算 1 轮（否则 rounds 永远是 0，指标无意义）
- 多轮循环里每轮调用 `begin_round()`，超限抛 `BudgetExhausted`
- deadline 必须真的能在超时后拒绝（不能只在 begin_round 时检查一次）
"""
from __future__ import annotations

import time

import pytest

from traceops.runtime.budget import BudgetExhausted, BudgetManager


class TestRoundGateIsReal:
    def test_begin_round_increments_rounds(self):
        budget = BudgetManager(max_rounds=3)
        budget.begin_round()
        assert budget.rounds == 1

    def test_exceeding_max_rounds_raises(self):
        budget = BudgetManager(max_rounds=3)
        for _ in range(3):
            budget.begin_round()
        with pytest.raises(BudgetExhausted, match="round"):
            budget.begin_round()

    def test_snapshot_reports_real_rounds(self):
        """快照里的 rounds 必须是真实值 —— worker 把它写进事件。"""
        budget = BudgetManager(max_rounds=5)
        budget.begin_round()
        budget.begin_round()
        assert budget.snapshot()["rounds"] == 2


class TestDeadlineGateIsReal:
    def test_deadline_rejects_after_expiry(self):
        budget = BudgetManager(deadline_seconds=0.05)
        time.sleep(0.1)
        with pytest.raises(BudgetExhausted, match="deadline"):
            budget.begin_round()

    def test_deadline_not_expired_allows_work(self):
        budget = BudgetManager(deadline_seconds=10)
        budget.begin_round()  # 不应抛


class TestGatesAreActuallyCalledDuringInvestigation:
    """真正的缺陷是**没被调用** —— 单元测试通过不代表闸门生效。"""

    def test_investigation_reports_at_least_one_round(self):
        """一次调查至少要算 1 轮。

        此前 rounds 恒为 0，而它被写进 investigation.completed 事件 ——
        消费方（Java Finding）拿到的用量数据里有假值。
        """
        from datetime import UTC, datetime, timedelta

        from traceops.evidence.store import EvidenceStore
        from traceops.orchestration.graph import (
            InvestigationRequest, RunContext, run_investigation,
        )

        now = datetime.now(UTC)
        ctx = RunContext(store=EvidenceStore(), adapters=None,
                         budget=BudgetManager(max_tool_calls=40))
        result = run_investigation(_make_request(now), ctx)
        assert ctx.budget.rounds >= 1, (
            "a completed investigation must count at least one round; "
            f"got {ctx.budget.rounds}")

    def test_round_budget_stops_the_investigation(self):
        """轮数上限必须能在真实调查里触发，而不是只在单测里。"""
        from datetime import UTC, datetime

        from traceops.evidence.store import EvidenceStore
        from traceops.orchestration.graph import RunContext, run_investigation

        now = datetime.now(UTC)
        ctx = RunContext(store=EvidenceStore(), adapters=None,
                         budget=BudgetManager(max_tool_calls=40, max_rounds=1))
        result = run_investigation(_make_request(now), ctx)
        # max_rounds=1 时，7 节点图必然超限 → 要么 FAILED，要么明确记录预算耗尽
        assert result.status == "FAILED" or any(
            "round" in e.lower() or "budget" in e.lower() for e in result.errors
        ), f"round gate did not fire: status={result.status} errors={result.errors}"


def _make_request(now):
    from datetime import timedelta

    from traceops.orchestration.graph import InvestigationRequest

    return InvestigationRequest(
        investigation_run_id="run-budget", incident_id="inc-budget",
        environment="demo", service_hint="order-service",
        fault_start=now, window_start=now - timedelta(minutes=2), window_end=now,
    )
