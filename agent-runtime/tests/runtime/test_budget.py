"""Cycle 4 RED: Budget Manager tests.

契约来源: REVISIONS-1.1 R6 —
执行逻辑: 调用模型前检查预算 → 预留 → 调用 → 取实际用量 → 扣减记账
→ 超预算则不再发起下一次模型调用。
Tool Call / Rounds / Deadline 同样在此管理（Tool Gateway 只管工具侧限制）。
"""
from __future__ import annotations

import time

import pytest

from traceops.runtime.budget import BudgetExhausted, BudgetManager


class TestTokenReservation:
    def test_reserve_and_settle(self):
        bm = BudgetManager(max_tokens=100_000)
        r = bm.reserve(estimate=10_000, purpose="planner")
        assert r.estimate == 10_000
        bm.settle(r, actual=8_000)
        assert bm.tokens_used == 8_000
        assert bm.tokens_remaining == 92_000

    def test_settle_actual_over_estimate(self):
        bm = BudgetManager(max_tokens=100_000)
        r = bm.reserve(estimate=10_000, purpose="investigator")
        bm.settle(r, actual=15_000)  # actual may exceed estimate; call already happened
        assert bm.tokens_used == 15_000

    def test_reserve_beyond_remaining_rejected_before_call(self):
        bm = BudgetManager(max_tokens=100_000)
        r = bm.reserve(estimate=90_000, purpose="a")
        bm.settle(r, actual=90_000)
        with pytest.raises(BudgetExhausted, match="token"):
            bm.reserve(estimate=20_000, purpose="b")

    def test_outstanding_reservations_count_against_budget(self):
        bm = BudgetManager(max_tokens=100_000)
        bm.reserve(estimate=60_000, purpose="a")  # outstanding, not yet settled
        with pytest.raises(BudgetExhausted):
            bm.reserve(estimate=60_000, purpose="b")  # 60k outstanding + 60k > 100k

    def test_release_unsettled_reservation(self):
        bm = BudgetManager(max_tokens=100_000)
        r = bm.reserve(estimate=60_000, purpose="a")
        bm.release(r)  # e.g. call failed before executing
        r2 = bm.reserve(estimate=60_000, purpose="b")  # fits again
        bm.settle(r2, actual=1_000)
        assert bm.tokens_used == 1_000


class TestToolCallAndRounds:
    def test_tool_call_budget(self):
        bm = BudgetManager(max_tokens=100_000, max_tool_calls=3)
        for _ in range(3):
            bm.consume_tool_call()
        with pytest.raises(BudgetExhausted, match="tool call"):
            bm.consume_tool_call()

    def test_round_budget(self):
        bm = BudgetManager(max_tokens=100_000, max_rounds=2)
        bm.begin_round()
        bm.begin_round()
        with pytest.raises(BudgetExhausted, match="round"):
            bm.begin_round()

    def test_deadline(self):
        bm = BudgetManager(max_tokens=100_000, deadline_seconds=0.05)
        time.sleep(0.06)
        with pytest.raises(BudgetExhausted, match="deadline"):
            bm.begin_round()


class TestSnapshot:
    def test_usage_snapshot_for_persistence(self):
        bm = BudgetManager(max_tokens=100_000, max_tool_calls=10, max_rounds=5)
        r = bm.reserve(estimate=5_000, purpose="planner")
        bm.settle(r, actual=4_321)
        bm.consume_tool_call()
        bm.begin_round()
        snap = bm.snapshot()
        assert snap["tokens_used"] == 4_321
        assert snap["tool_calls"] == 1
        assert snap["rounds"] == 1
        assert snap["limits"] == {"tokens": 100_000, "tool_calls": 10, "rounds": 5}
        assert len(snap["ledger"]) == 1
        assert snap["ledger"][0]["purpose"] == "planner"
        assert snap["ledger"][0]["actual"] == 4_321

    def test_default_budget_is_100k(self):
        bm = BudgetManager()
        assert bm.max_tokens == 100_000
