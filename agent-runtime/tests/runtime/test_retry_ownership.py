"""重试归谁管？

**控制面管重试，agent 只负责说清"这次能不能重试"。**

这个测试锁住这个分工，防止又滑回"两边都以为对方在管"：

- agent 侧的 `is_retryable(reason)` 是**分类器**：把 failure_reason
  分成"这类失败重试有意义"和"重试只是烧钱"。它必须在 worker 里
  真正被调用（worker 发失败事件时要填 `retryable`）。
- agent 侧**不该**再有"要不要重试、什么时候重试、重试几次"的策略对象：
  次数和退避必须由控制面决定，因为事故状态机和持久化 outbox 都在那边，
  agent 进程重启就没了它那份计数。
  控制面真的这么做了（见 `InvestigationRetryService`）。

所以：worker 上不能挂着没人用的重试策略，CLI 也不能再传它。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _module_source(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


class TestAgentOnlyClassifies:
    def test_is_retryable_is_used_by_the_worker(self):
        """分类器必须在生产路径上被调用，否则又是一段死代码。"""
        source = _module_source("agent-runtime/traceops/runtime/worker.py")
        assert "is_retryable(" in source, (
            "worker 必须调用 is_retryable 来决定失败事件里的 retryable 字段")

    def test_the_worker_still_fills_the_retryable_field(self):
        """worker 发出的失败事件必须带 retryable —— 控制面靠它决定重试。"""
        source = _module_source("agent-runtime/traceops/runtime/worker.py")
        assert "retryable=" in source


class TestAgentDoesNotOwnTheRetryLoop:
    def test_the_worker_has_no_retry_policy_parameter(self):
        """worker 构造函数不再接受 retry_policy。

        留着它就意味着"看起来 agent 也能决定重试"，而实际上没人用 ——
        这正是之前 `_retry_decision` 的状态（零调用者）。
        """
        tree = ast.parse(_module_source("agent-runtime/traceops/runtime/worker.py"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name != "__init__":
                continue
            args = node.args
            names = [a.arg for a in list(args.args) + list(args.kwonlyargs)]
            assert "retry_policy" not in names, (
                "worker 不该再持有 retry_policy：重试由控制面决定")

    def test_the_worker_has_no_dead_retry_decision_helper(self):
        """`_retry_decision` 曾经零调用者 —— 删掉，而不是留着当装饰。"""
        tree = ast.parse(_module_source("agent-runtime/traceops/runtime/worker.py"))
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert "_retry_decision" not in names

    def test_the_cli_does_not_pass_a_retry_policy(self):
        """CLI 还在传 RetryPolicy(max_attempts=3) —— 那是控制面的活。"""
        source = _module_source("agent-runtime/traceops/cli/worker.py")
        assert "retry_policy=" not in source
        assert "RetryPolicy" not in source


class TestTheClassifierStillExists:
    def test_the_reason_classification_is_unchanged(self):
        """分类本身没变：控制面换了主，但"什么能重试"的判断还归 agent。"""
        from traceops.runtime.retry import is_retryable

        assert is_retryable("INTERNAL_ERROR")
        assert is_retryable("MODEL_TIMEOUT")
        assert not is_retryable("BUDGET_EXHAUSTED")
        assert not is_retryable("INVALID_REQUEST")
        assert not is_retryable("SOMETHING_UNKNOWN")
