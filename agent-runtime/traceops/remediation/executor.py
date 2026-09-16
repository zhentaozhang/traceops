"""受控执行（蓝皮书 §9.2 / §9.4）。

执行顺序是硬约束，任何一步失败都停在原地：

    1. policy.allowed        —— 不合法 → BLOCKED（审批无权豁免）
    2. approval != null 且 APPROVED（若 requires_approval）
                             —— 未批/被拒 → BLOCKED
    3. approval 与 plan 内容哈希一致
                             —— 被改过 → BLOCKED
    4. executor.execute      —— 只有这里才会真正动环境
    5. verifier.verify       —— 执行成功 ≠ 业务恢复

第 5 步失败时返回 NOT_VERIFIED + rollback_required=True，
**不自动回滚**：回滚本身是另一个有风险的动作，必须重新走同样流程。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from traceops.remediation.actions import ActionRegistry
from traceops.remediation.approval import Approval, ApprovalDecision, PlanIntegrityError, verify_approval_matches
from traceops.remediation.plan import RemediationPlan
from traceops.remediation.policy import PolicyDecision


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    FAILED = "failed"
    NOT_VERIFIED = "not_verified"
    # 预演通过（检查全过、但**没有执行**）。单独一个状态而不是 SUCCESS：
    # 把"预演通过"记成成功会让人误以为环境已经被改动。
    DRY_RUN = "dry_run"


@dataclass
class ExecutionResult:
    status: ExecutionStatus
    attempts: int
    post_conditions_met: bool
    rollback_required: bool
    reasons: list[str]
    error: str | None = None


def execute_plan(
    plan: RemediationPlan,
    *,
    policy: PolicyDecision,
    approval: Approval | None,
    registry: ActionRegistry,
    executor,
    verifier,
    dry_run: bool = False,
) -> ExecutionResult:
    reasons: list[str] = []

    # 1. 策略优先：审批不能洗白违规
    if not policy.allowed:
        return ExecutionResult(
            status=ExecutionStatus.BLOCKED,
            attempts=0,
            post_conditions_met=False,
            rollback_required=False,
            reasons=["policy denied: " + "; ".join(policy.reasons)],
        )

    # 2. 审批要求
    if policy.requires_approval:
        if approval is None:
            return ExecutionResult(
                status=ExecutionStatus.BLOCKED,
                attempts=0,
                post_conditions_met=False,
                rollback_required=False,
                reasons=["approval required but missing"],
            )
        if approval.decision is not ApprovalDecision.APPROVED:
            return ExecutionResult(
                status=ExecutionStatus.BLOCKED,
                attempts=0,
                post_conditions_met=False,
                rollback_required=False,
                reasons=[f"approval decision was {approval.decision.value}"],
            )

    # 3. 审批绑定内容
    if approval is not None:
        try:
            verify_approval_matches(approval, plan)
        except PlanIntegrityError as exc:
            return ExecutionResult(
                status=ExecutionStatus.BLOCKED,
                attempts=0,
                post_conditions_met=False,
                rollback_required=False,
                reasons=[str(exc)],
            )
        reasons.append(f"approved by {approval.approver}")

    # 4. 执行（唯一会改动环境的位置）
    #
    # dry_run 只在这里短路：前面的策略/审批/哈希检查**全部照样跑**。
    # 如果 dry run 跳过检查，它就变成了"先试后批"的后门。
    if dry_run:
        return ExecutionResult(
            status=ExecutionStatus.DRY_RUN,
            attempts=0,
            post_conditions_met=False,
            rollback_required=False,
            reasons=reasons + ["dry run: all guards passed, nothing executed"],
        )

    try:
        executor.execute(plan.action, plan.target, plan.parameters)
    except Exception as exc:
        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            attempts=1,
            post_conditions_met=False,
            rollback_required=False,
            reasons=reasons,
            error=str(exc),
        )

    # 5. 后置校验
    recovered = bool(verifier.verify(plan)) if plan.post_conditions else True
    if not recovered:
        return ExecutionResult(
            status=ExecutionStatus.NOT_VERIFIED,
            attempts=1,
            post_conditions_met=False,
            rollback_required=True,
            reasons=reasons + ["post conditions not met after execution"],
        )

    return ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        attempts=1,
        post_conditions_met=recovered,
        rollback_required=False,
        reasons=reasons + ["post conditions satisfied"],
    )
