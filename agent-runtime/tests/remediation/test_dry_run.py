"""Dry Run：预演一个计划而不动环境。

## 为什么 P3 必须有它（PRD 5.5 的未实现项）

`execute_plan` 的第 4 步是**唯一**会改动环境的位置。没有 Dry Run 时，
一次"我想看看会发生什么"的试探要么：
- 真执行（危险）
- 什么都不做（什么都没验证到）

Dry Run 的价值不是"少执行一次"，而是**用同一套闸门预演**：
策略、审批、plan 哈希、目标白名单**全部照样检查**，只是把
`executor.execute` 换成空操作。这样运维能在批准之前就发现
"这个计划其实过不了策略"或"审批哈希对不上"。

## 铁律：Dry Run 不能变成绕过检查的捷径

如果 Dry Run 跳过策略或审批检查，它就成了"先试后批"的后门。
"""
from __future__ import annotations

import pytest

from traceops.remediation.actions import ActionRegistry, ActionSpec, RiskLevel
from traceops.remediation.approval import Approval, ApprovalDecision, approve
from traceops.remediation.executor import execute_plan
from traceops.remediation.plan import RemediationPlan
from traceops.remediation.policy import PolicyDecision


def _registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="scale_pool",
        description="resize a connection pool",
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        allowed_targets={"redis-connection-pool"},
        parameter_schema={
            "type": "object",
            "properties": {"max_pool_size": {"type": "integer", "minimum": 1}},
            "required": ["max_pool_size"],
            "additionalProperties": False,
        },
        post_conditions=["error_rate_below_threshold"],
    ))
    return registry


def _plan(**overrides) -> RemediationPlan:
    base = dict(
        plan_id="plan-1", incident_id="inc-1", action="scale_pool",
        target="redis-connection-pool", parameters={"max_pool_size": 20},
        risk=RiskLevel.MEDIUM, requires_approval=True,
        rationale="restore maxPoolSize after config change",
        rollback={"action": "scale_pool", "parameters": {"max_pool_size": 2}},
    )
    base.update(overrides)
    return RemediationPlan(**base)


class RecordingExecutor:
    """记录是否被真的调用 —— Dry Run 的核心断言就是它没被调过。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def execute(self, action, target, parameters):
        self.calls.append((action, target, dict(parameters)))


class AlwaysHealthyVerifier:
    def __init__(self):
        self.calls = 0

    def verify(self, plan):
        self.calls += 1
        return True


class TestDryRunChecksEverything:
    def test_dry_run_does_not_touch_the_environment(self):
        executor = RecordingExecutor()
        result = execute_plan(
            _plan(), policy=PolicyDecision(allowed=True, reasons=[], requires_approval=True),
            approval=approve(_plan(), approver="sre-1",
                             decision=ApprovalDecision.APPROVED, reason="restore pool"),
            registry=_registry(), executor=executor, verifier=AlwaysHealthyVerifier(),
            dry_run=True,
        )
        assert result.status.value == "dry_run"
        assert executor.calls == [], "dry run must not execute anything"

    def test_dry_run_still_enforces_policy(self):
        """策略拒绝时，Dry Run 也必须报 BLOCKED —— 否则它成了绕过检查的后门。"""
        executor = RecordingExecutor()
        result = execute_plan(
            _plan(), policy=PolicyDecision(allowed=False, reasons=["high risk action"], requires_approval=False),
            approval=None, registry=_registry(), executor=executor,
            verifier=AlwaysHealthyVerifier(), dry_run=True,
        )
        assert result.status.value == "blocked"
        assert executor.calls == []

    def test_dry_run_still_requires_approval(self):
        executor = RecordingExecutor()
        result = execute_plan(
            _plan(), policy=PolicyDecision(allowed=True, reasons=[], requires_approval=True),
            approval=None, registry=_registry(), executor=executor,
            verifier=AlwaysHealthyVerifier(), dry_run=True,
        )
        assert result.status.value == "blocked"
        assert "approval" in str(result.reasons).lower()

    def test_dry_run_still_checks_plan_hash(self):
        """审批绑定内容哈希：Dry Run 也必须发现"批了 A 却要执行 B"。"""
        from datetime import UTC, datetime

        plan = _plan()
        mismatched = Approval(
            plan_id=plan.plan_id, plan_hash="deadbeef", approver="sre-1",
            decision=ApprovalDecision.APPROVED, reason="approved a different plan",
            decided_at=datetime.now(UTC),
        )
        executor = RecordingExecutor()
        result = execute_plan(
            plan, policy=PolicyDecision(allowed=True, reasons=[], requires_approval=True),
            approval=mismatched, registry=_registry(), executor=executor,
            verifier=AlwaysHealthyVerifier(), dry_run=True,
        )
        assert result.status.value == "blocked"
        assert executor.calls == []

    def test_successful_dry_run_does_not_run_verifier(self):
        """没有真正执行，就没有"执行后恢复"可验证。"""
        verifier = AlwaysHealthyVerifier()
        result = execute_plan(
            _plan(), policy=PolicyDecision(allowed=True, reasons=[], requires_approval=True),
            approval=approve(_plan(), approver="sre-1",
                             decision=ApprovalDecision.APPROVED, reason="restore pool"),
            registry=_registry(), executor=RecordingExecutor(), verifier=verifier,
            dry_run=True,
        )
        assert verifier.calls == 0, "dry run must not claim post-condition verification"


class TestDryRunIsExplicit:
    def test_default_is_not_dry_run(self):
        """默认必须是**真执行**：dry run 要显式要求，不能悄悄变成默认。"""
        executor = RecordingExecutor()
        execute_plan(
            _plan(), policy=PolicyDecision(allowed=True, reasons=[], requires_approval=True),
            approval=approve(_plan(), approver="sre-1",
                             decision=ApprovalDecision.APPROVED, reason="restore pool"),
            registry=_registry(), executor=executor, verifier=AlwaysHealthyVerifier(),
        )
        assert executor.calls, "without dry_run=True the executor must run"
