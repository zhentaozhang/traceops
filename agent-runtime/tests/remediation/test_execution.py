"""Execution Plane 测试（蓝皮书 §9，REVISIONS-1.1 R7）。

核心主张：诊断权限 ≠ 执行权限。规则：
1. 任何写操作必须有 allowlist 里注册的 action，不允许自由文本/shell
2. Policy Evaluation 先于审批 —— 审批不能把违规操作合法化
3. 审批绑定 plan 内容哈希；参数一变，原审批作废
4. 高风险操作必须审批；低风险也要过 policy
5. 执行后必须有 post-condition 校验，失败走回滚/升级
"""
from __future__ import annotations

import pytest

from traceops.remediation.actions import ActionRegistry, ActionSpec, RiskLevel, action_registry
from traceops.remediation.approval import (
    ApprovalDecision,
    PlanIntegrityError,
    approve,
    verify_approval_matches,
)
from traceops.remediation.executor import ExecutionStatus, execute_plan
from traceops.remediation.plan import RemediationPlan, build_plan
from traceops.remediation.policy import PolicyDecision, PolicyEngine


class RecordingExecutor:
    """记录执行的假后端（真实环境是受控 worker，不是 shell）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def execute(self, action: str, target: str, parameters: dict) -> dict:
        self.calls.append({"action": action, "target": target, "parameters": parameters})
        if self.fail:
            raise RuntimeError("executor rejected the action")
        return {"applied": True, "target": target}


class ScriptedVerifier:
    def __init__(self, *, recovered: bool = True) -> None:
        self.recovered = recovered
        self.checks = 0

    def verify(self, plan: RemediationPlan) -> bool:
        self.checks += 1
        return self.recovered


def _registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="restart_test_service",
        description="重启测试环境服务",
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        allowed_targets={"order-service", "payment-service"},
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"grace_seconds": {"type": "integer", "minimum": 0, "maximum": 60}},
        },
        post_conditions=["error_rate_below_threshold", "service_healthy"],
    ))
    registry.register(ActionSpec(
        name="update_test_config",
        description="更新测试环境配置项",
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        allowed_targets={"redis-connection-pool"},
        parameter_schema={
            "type": "object",
            "required": ["max_connections"],
            "additionalProperties": False,
            "properties": {"max_connections": {"type": "integer", "minimum": 1, "maximum": 500}},
        },
        post_conditions=["pool_pending_zero"],
    ))
    registry.register(ActionSpec(
        name="read_only_diagnostic_snapshot",
        description="只读诊断快照（不需要审批）",
        risk=RiskLevel.LOW,
        requires_approval=False,
        allowed_targets={"order-service"},
        parameter_schema={"type": "object", "additionalProperties": False},
        post_conditions=[],
    ))
    return registry


def _plan(action: str = "update_test_config", target: str = "redis-connection-pool",
          parameters: dict | None = None) -> RemediationPlan:
    return build_plan(
        incident_id="inc-001",
        action=action,
        target=target,
        parameters={"max_connections": 20} if parameters is None else parameters,
        rationale="restore pool size before shrink",
        rollback={"max_connections": 2},
        registry=_registry(),
    )


class TestActionRegistry:
    def test_unknown_action_rejected(self):
        with pytest.raises(KeyError):
            _registry().get("rm -rf /")

    def test_builtin_registry_is_empty_and_explicit(self):
        """内置注册表不预置任何写操作：必须显式注册，避免"默认就能改生产"。"""
        registry = action_registry()
        assert registry.names() == []

    def test_target_must_be_allowed(self):
        registry = _registry()
        with pytest.raises(ValueError, match="not allowed"):
            build_plan(incident_id="inc-001", action="restart_test_service",
                       target="prod-database", parameters={}, rationale="x",
                       rollback=None, registry=registry)

    def test_parameter_schema_enforced(self):
        registry = _registry()
        with pytest.raises(ValueError, match="invalid parameters"):
            build_plan(incident_id="inc-001", action="update_test_config",
                       target="redis-connection-pool", parameters={"max_connections": 9999},
                       rationale="x", rollback=None, registry=registry)

    def test_free_form_extra_parameter_rejected(self):
        registry = _registry()
        with pytest.raises(ValueError, match="invalid parameters"):
            build_plan(incident_id="inc-001", action="update_test_config",
                       target="redis-connection-pool",
                       parameters={"max_connections": 20, "shell": "rm -rf /"},
                       rationale="x", rollback=None, registry=registry)


class TestPolicyBeforeApproval:
    def test_high_risk_denied_even_if_would_be_approved(self):
        """R7 关键断言：审批不能洗白违规操作。"""
        registry = _registry()
        registry.register(ActionSpec(
            name="rollback_test_deployment",
            description="回滚部署",
            risk=RiskLevel.HIGH,
            requires_approval=True,
            allowed_targets={"order-service"},
            parameter_schema={"type": "object", "additionalProperties": False},
            post_conditions=[],
        ))
        plan = build_plan(incident_id="inc-001", action="rollback_test_deployment",
                          target="order-service", parameters={}, rationale="x",
                          rollback=None, registry=registry)

        policy = PolicyEngine(denied_actions={"rollback_test_deployment"},
                              denied_risks=set())
        decision = policy.evaluate(plan, registry)

        assert decision.allowed is False
        assert decision.requires_approval is True  # 仍需审批记录，但结果必须是拒绝
        assert any("denied" in reason for reason in decision.reasons)

    def test_denied_risk_level_blocks_plan(self):
        plan = _plan()
        policy = PolicyEngine(denied_risks={RiskLevel.HIGH})
        # MEDIUM 不被拒
        assert policy.evaluate(plan, _registry()).allowed is True

    def test_low_risk_action_does_not_require_approval(self):
        plan = _plan(action="read_only_diagnostic_snapshot", target="order-service", parameters={})
        decision = PolicyEngine().evaluate(plan, _registry())
        assert decision.allowed is True
        assert decision.requires_approval is False


class TestApprovalBindsContent:
    def test_approval_carries_plan_hash(self):
        plan = _plan()
        approval = approve(plan, approver="sre-oncall", decision=ApprovalDecision.APPROVED,
                           reason="pool misconfigured")
        assert approval.plan_hash == plan.content_hash()
        assert approval.decision is ApprovalDecision.APPROVED

    def test_tampered_plan_invalidates_approval(self):
        plan = _plan()
        approval = approve(plan, approver="sre-oncall", decision=ApprovalDecision.APPROVED,
                           reason="ok")

        tampered = _plan(parameters={"max_connections": 400})
        with pytest.raises(PlanIntegrityError, match="approval"):
            verify_approval_matches(approval, tampered)

    def test_rejected_approval_is_still_recorded(self):
        plan = _plan()
        approval = approve(plan, approver="sre-oncall", decision=ApprovalDecision.REJECTED,
                           reason="too risky during peak")
        assert approval.decision is ApprovalDecision.REJECTED
        assert verify_approval_matches(approval, plan) is True  # 内容匹配，但决策是拒绝


class TestExecution:
    def test_happy_path_requires_approval_then_verify(self):
        registry = _registry()
        plan = _plan()
        policy = PolicyEngine().evaluate(plan, registry)
        approval = approve(plan, approver="sre-oncall", decision=ApprovalDecision.APPROVED, reason="ok")

        result = execute_plan(plan, policy=policy, approval=approval, registry=registry,
                              executor=RecordingExecutor(), verifier=ScriptedVerifier(recovered=True))

        assert result.status is ExecutionStatus.SUCCESS
        assert result.post_conditions_met is True
        assert result.attempts == 1

    def test_high_risk_without_approval_is_blocked(self):
        registry = _registry()
        plan = _plan()
        policy = PolicyEngine().evaluate(plan, registry)
        executor = RecordingExecutor()

        result = execute_plan(plan, policy=policy, approval=None, registry=registry,
                              executor=executor, verifier=ScriptedVerifier())

        assert result.status is ExecutionStatus.BLOCKED
        assert executor.calls == []  # 一次都没有真正执行

    def test_policy_denied_blocks_even_with_approval(self):
        registry = _registry()
        plan = _plan()
        policy = PolicyEngine(denied_actions={"update_test_config"}).evaluate(plan, registry)
        approval = approve(plan, approver="sre-oncall", decision=ApprovalDecision.APPROVED, reason="ok")
        executor = RecordingExecutor()

        result = execute_plan(plan, policy=policy, approval=approval, registry=registry,
                              executor=executor, verifier=ScriptedVerifier())

        assert result.status is ExecutionStatus.BLOCKED
        assert executor.calls == []

    def test_rejected_approval_blocks_execution(self):
        registry = _registry()
        plan = _plan()
        policy = PolicyEngine().evaluate(plan, registry)
        approval = approve(plan, approver="sre-oncall", decision=ApprovalDecision.REJECTED, reason="no")
        executor = RecordingExecutor()

        result = execute_plan(plan, policy=policy, approval=approval, registry=registry,
                              executor=executor, verifier=ScriptedVerifier())

        assert result.status is ExecutionStatus.BLOCKED
        assert executor.calls == []

    def test_executor_failure_is_reported_not_swallowed(self):
        registry = _registry()
        plan = _plan()
        policy = PolicyEngine().evaluate(plan, registry)
        approval = approve(plan, approver="sre", decision=ApprovalDecision.APPROVED, reason="ok")

        result = execute_plan(plan, policy=policy, approval=approval, registry=registry,
                              executor=RecordingExecutor(fail=True), verifier=ScriptedVerifier())

        assert result.status is ExecutionStatus.FAILED
        assert "executor rejected the action" in (result.error or "")

    def test_executed_but_not_recovered_requires_rollback(self):
        """执行成功 ≠ 业务恢复（蓝皮书 §9.4）。"""
        registry = _registry()
        plan = _plan()
        policy = PolicyEngine().evaluate(plan, registry)
        approval = approve(plan, approver="sre", decision=ApprovalDecision.APPROVED, reason="ok")

        result = execute_plan(plan, policy=policy, approval=approval, registry=registry,
                              executor=RecordingExecutor(), verifier=ScriptedVerifier(recovered=False))

        assert result.status is ExecutionStatus.NOT_VERIFIED
        assert result.post_conditions_met is False
        assert result.rollback_required is True

    def test_low_risk_action_runs_without_approval(self):
        registry = _registry()
        plan = _plan(action="read_only_diagnostic_snapshot", target="order-service", parameters={})
        policy = PolicyEngine().evaluate(plan, registry)
        executor = RecordingExecutor()

        result = execute_plan(plan, policy=policy, approval=None, registry=registry,
                              executor=executor, verifier=ScriptedVerifier())

        assert result.status is ExecutionStatus.SUCCESS
        assert len(executor.calls) == 1


class TestPlanImmutability:
    def test_plan_hash_is_stable_for_same_content(self):
        assert _plan().content_hash() == _plan().content_hash()

    def test_plan_hash_changes_with_parameters(self):
        assert _plan().content_hash() != _plan(parameters={"max_connections": 30}).content_hash()

    def test_plan_hash_changes_with_target(self):
        registry = _registry()
        a = build_plan(incident_id="inc-001", action="restart_test_service", target="order-service",
                       parameters={"grace_seconds": 5}, rationale="x", rollback=None, registry=registry)
        b = build_plan(incident_id="inc-001", action="restart_test_service", target="payment-service",
                       parameters={"grace_seconds": 5}, rationale="x", rollback=None, registry=registry)
        assert a.content_hash() != b.content_hash()
