"""调查必须有**墙钟上限**，否则可以无限期运行。

## 缺陷（搜索 PEL 恢复实践时发现）

`BudgetManager` 声明了 `deadline_seconds`，`_check_deadline()` 也实现了检查，
但**全仓没有任何调用点设置它**（grep `deadline_seconds=` 除定义处零命中）。

后果是链式的，而不只是"跑久了点"：

1. **worker 可以被一个病态调查永久占住**。数据源持续返回慢响应时，
   `max_tool_calls=50` 能拦住工具次数，但拦不住**每次都很慢**的 50 次调用。
2. **PEL 恢复参数无法定标**。`min_idle_ms=60s` 的作用是"消息空闲超过这么久
   才认为原消费者死了"。但如果调查本身可能跑 10 分钟，
   60 秒就会**误抢**正在正常处理的活 —— 于是同一个事故被两个 worker 同时查。
3. **优雅停机被架空**。SIGTERM 后要"处理完当前批次"，而一个没有上限的调查
   可以让这批次永远不结束，最后仍被宽限期硬杀。

## 三条约束

- 必须有默认值（不能靠"记得配"）
- 必须**真的接进** BudgetManager 的构造（否则还是死代码）
- 超时必须是**干净失败**（返回 FAILED 结果，不是抛异常穿透）
"""
from __future__ import annotations

import time

import pytest

from traceops.runtime.budget import BudgetExhausted, BudgetManager


class TestDefaultDeadlineExists:
    def test_budget_has_a_default_deadline(self):
        budget = BudgetManager()
        assert budget.deadline_seconds is not None, (
            "a budget without a wall-clock limit lets one pathological "
            "investigation occupy a worker forever")
        assert budget.deadline_seconds > 0

    def test_default_is_generous_but_bounded(self):
        """默认值要能容纳正常调查，又远小于"永远"。"""
        budget = BudgetManager()
        assert 60 <= budget.deadline_seconds <= 1800, (
            f"unreasonable default deadline: {budget.deadline_seconds}")


class TestDeadlineActuallyFires:
    def test_expired_deadline_raises_on_round(self):
        budget = BudgetManager(deadline_seconds=0.05)
        time.sleep(0.1)
        with pytest.raises(BudgetExhausted, match="deadline"):
            budget.begin_round()

    def test_explicit_none_disables_it_only_when_asked(self):
        """允许显式关闭（长时间批处理），但必须是显式选择。"""
        budget = BudgetManager(deadline_seconds=None)
        assert budget.deadline_seconds is None


class TestInvestigationJobIsDeadlineBounded:
    def test_run_investigation_uses_a_bounded_budget(self):
        """真实入口构造的预算必须带 deadline（防止只改默认值但没人用）。"""
        import inspect

        from traceops.cli.worker import _build_budget

        source = inspect.getsource(_build_budget)
        assert "deadline_seconds" in source, (
            "the production budget factory must set a deadline explicitly")
