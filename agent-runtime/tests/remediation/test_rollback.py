"""回滚：执行成功但**业务未恢复**时必须能退回去。

## PRD 现状（5.5 的未实现项）

`execute_plan` 第 5 步失败时返回 `NOT_VERIFIED + rollback_required=True`，
注释写得很清楚：「**不自动回滚**：回滚本身是另一个有风险的动作，
必须重新走同样流程」。

这个决定是对的，但它留下一个空洞：`RemediationPlan.rollback` 字段
**存在却从未被使用** —— 也就是说"必须重新走流程"时，
没有任何东西把"退回去该做什么"变成流程能用的东西。

## 正确设计（不是自动回滚）

回滚计划必须是**一等公民**：能从原计划派生、能被同样的策略/审批校验、
并且明确拒绝"没有回滚方案的高风险动作"。

这样 `rollback_required` 才不是一句空话 —— 运维拿到的是一个
**可直接提交审批的回滚计划**，而不是自己临时想怎么退。
"""
from __future__ import annotations

import pytest

from traceops.remediation.actions import ActionRegistry, ActionSpec, RiskLevel
from traceops.remediation.approval import ApprovalDecision, approve
from traceops.remediation.executor import execute_plan
from traceops.remediation.plan import RemediationPlan
from traceops.remediation.policy import PolicyDecision
from traceops.remediation.rollback import (
    RollbackUnavailable, build_rollback_plan, rollback_required_for,
)


def _registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="scale_pool", description="resize pool", risk=RiskLevel.MEDIUM,
        requires_approval=True, allowed_targets={"redis-connection-pool"},
        parameter_schema={"type": "object",
                          "properties": {"max_pool_size": {"type": "integer", "minimum": 1}},
                          "required": ["max_pool_size"], "additionalProperties": False},
        post_conditions=["error_rate_below_threshold"],
    ))
    registry.register(ActionSpec(
        name="restart_service", description="rolling restart", risk=RiskLevel.HIGH,
        requires_approval=True, allowed_targets={"order-service"},
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        post_conditions=["service_healthy"],
    ))
    return registry


def _plan(rollback=None, **overrides) -> RemediationPlan:
    base = dict(
        plan_id="plan-1", incident_id="inc-1", action="scale_pool",
        target="redis-connection-pool", parameters={"max_pool_size": 20},
        risk=RiskLevel.MEDIUM, requires_approval=True,
        rationale="restore maxPoolSize",
        rollback=rollback if rollback is not None else
        {"action": "scale_pool", "parameters": {"max_pool_size": 2}},
        post_conditions=["error_rate_below_threshold"],
    )
    base.update(overrides)
    return RemediationPlan(**base)


class TestBuildRollbackPlan:
    def test_rollback_plan_reverses_the_parameters(self):
        original = _plan()
        rollback = build_rollback_plan(original, registry=_registry(), reason="unverified")
        assert rollback.action == "scale_pool"
        assert rollback.parameters == {"max_pool_size": 2}
        assert rollback.target == "redis-connection-pool"

    def test_rollback_plan_has_its_own_content_hash(self):
        """回滚计划必须是独立计划：它的哈希不能等于原计划。"""
        original = _plan()
        rollback = build_rollback_plan(original, registry=_registry(), reason="unverified")
        assert rollback.content_hash() != original.content_hash()

    def test_rollback_plan_references_the_original(self):
        """审计要求：回滚计划要能指回它退的是哪个计划。"""
        rollback = build_rollback_plan(_plan(), registry=_registry(), reason="unverified")
        assert "plan-1" in rollback.rationale

    def test_no_rollback_section_raises(self):
        """没有回滚方案的动作，必须明确拒绝而不是编一个。"""
        original = _plan(rollback=None, action="restart_service",
                         target="order-service", parameters={}, risk=RiskLevel.HIGH)
        # 显式构造一个 rollback=None 的计划
        no_rollback = RemediationPlan(
            plan_id="p2", incident_id="inc-1", action="restart_service",
            target="order-service", parameters={}, risk=RiskLevel.HIGH,
            requires_approval=True, rationale="restart", rollback=None,
        )
        with pytest.raises(RollbackUnavailable):
            build_rollback_plan(no_rollback, registry=_registry(), reason="unverified")

    def test_rollback_of_rollback_is_the_original(self):
        """回滚的计划本身也可以回滚（回到原参数）。"""
        original = _plan()
        first = build_rollback_plan(original, registry=_registry(), reason="unverified")
        second = build_rollback_plan(first, registry=_registry(), reason="re-rollback")
        assert second.parameters == original.parameters

    def test_rollback_plan_does_not_claim_original_post_conditions(self):
        """回滚的目标不是"原计划的后置条件达成"，而是"恢复到能观测的状态"。

        沿用原后置条件会让回滚永远 NOT_VERIFIED（因为原动作本身没成功）。
        """
        original = _plan()
        rollback = build_rollback_plan(original, registry=_registry(), reason="unverified")
        assert rollback.post_conditions != original.post_conditions or not rollback.post_conditions


class TestRollbackRequiresApproval:
    def test_rollback_plan_goes_through_the_same_gates(self):
        """回滚是另一个写操作，必须过策略 + 审批 + 哈希，不能自豁免。"""
        rollback = build_rollback_plan(_plan(), registry=_registry(), reason="unverified")
        result = execute_plan(
            rollback, policy=PolicyDecision(allowed=True, reasons=[], requires_approval=True),
            approval=None, registry=_registry(), executor=_Recorder(),
            verifier=_Healthy(),
        )
        assert result.status.value == "blocked"
        assert "approval" in str(result.reasons).lower()

    def test_approved_rollback_executes(self):
        rollback = build_rollback_plan(_plan(), registry=_registry(), reason="unverified")
        recorder = _Recorder()
        result = execute_plan(
            rollback, policy=PolicyDecision(allowed=True, reasons=[], requires_approval=True),
            approval=approve(rollback, approver="sre-1",
                             decision=ApprovalDecision.APPROVED, reason="rollback approved"),
            registry=_registry(), executor=recorder, verifier=_Healthy(),
        )
        assert result.status.value == "success"
        assert recorder.calls

    def test_high_risk_rollback_blocked_by_policy_even_if_approved(self):
        """高风险动作被策略禁止时，审批也救不了 —— 回滚同样受这条约束。"""
        rollback = build_rollback_plan(_plan(), registry=_registry(), reason="unverified")
        result = execute_plan(
            rollback,
            policy=PolicyDecision(allowed=False, reasons=["high risk"], requires_approval=False),
            approval=approve(rollback, approver="admin",
                             decision=ApprovalDecision.APPROVED, reason="approved"),
            registry=_registry(), executor=_Recorder(), verifier=_Healthy(),
        )
        assert result.status.value == "blocked"


class TestRollbackRequiredSignal:
    def test_not_verified_marks_rollback_required(self):
        plan = _plan()
        result = execute_plan(
            plan, policy=PolicyDecision(allowed=True, reasons=[], requires_approval=True),
            approval=approve(plan, approver="sre-1",
                             decision=ApprovalDecision.APPROVED, reason="go"),
            registry=_registry(), executor=_Recorder(), verifier=_Unhealthy(),
        )
        assert result.status.value == "not_verified"
        assert result.rollback_required is True

    def test_rollback_required_for_is_true_only_when_needed(self):
        plan = _plan()
        result = execute_plan(
            plan, policy=PolicyDecision(allowed=True, reasons=[], requires_approval=True),
            approval=approve(plan, approver="sre-1",
                             decision=ApprovalDecision.APPROVED, reason="go"),
            registry=_registry(), executor=_Recorder(), verifier=_Healthy(),
        )
        assert rollback_required_for(result) is False


class _Recorder:
    def __init__(self):
        self.calls = []

    def execute(self, action, target, parameters):
        self.calls.append((action, target, dict(parameters)))


class _Healthy:
    def verify(self, plan):
        return True


class _Unhealthy:
    def verify(self, plan):
        return False
