"""受控执行（蓝皮书 §9.2 / §9.4）。

执行顺序是硬约束，任何一步失败都停在原地：

    1. policy.allowed        —— 不合法 → BLOCKED（审批无权豁免）
    2. approval != null 且 APPROVED（若 requires_approval）
                             —— 未批/被拒 → BLOCKED
    3. approval 与 plan 内容哈希一致
                             —— 被改过 → BLOCKED
    4. executor.execute      —— 只有这里才会真正动环境
    5. verifier.verify       —— 执行成功 ≠ 业务恢复

第 5 步失败时返回 NOT_VERIFIED + rollback_required=True。`execute_plan`
**自己不回滚** —— 回滚是另一个有风险的动作，必须重新过策略。需要闭环时用
`execute_with_rollback`：它拿同一个 `PolicyEngine` 重新评估回滚计划，
再用原审批（`content_hash` 包含 `rollback`，所以回滚在批准范围内）执行它。
"""
from __future__ import annotations

import logging

from dataclasses import dataclass
from enum import Enum

from traceops.remediation.actions import ActionRegistry
from traceops.remediation.approval import Approval, ApprovalDecision, PlanIntegrityError, verify_approval_matches
from traceops.remediation.plan import RemediationPlan
from traceops.remediation.policy import PolicyDecision, PolicyEngine




log = logging.getLogger(__name__)

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
    approval_for: RemediationPlan | None = None,
) -> ExecutionResult:
    """执行一份计划。

    `approval_for` 只给"执行某份计划的**声明回滚**"用：审批绑定的是原计划的
    内容哈希，而回滚计划是另一份计划（动作不同、参数不同），直接用原审批
    会被 `verify_approval_matches` 判成不匹配。由于 `content_hash` **包含**
    `rollback` 字段，批准原计划就等于批准了它写明的那次回滚 —— 所以这里
    允许把审批对到原计划上，而不是放宽审批要求。默认 None，普通路径不变。
    """
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
            verify_approval_matches(approval, approval_for or plan)
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
        # 写操作失败必须留痕：这是最需要事后追溯的一类失败
        # （谁批的、改了什么、报了什么错）。只写进 ExecutionResult.error
        # 的话，调用方不落盘就等于没发生。
        log.error("remediation execution failed",
                  extra={"action": plan.action,
                         "target": plan.target,
                         "plan_id": plan.plan_id,
                         "incident_id": plan.incident_id,
                         "error_type": type(exc).__name__,
                         "reason": str(exc)},
                  exc_info=True)
        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            attempts=1,
            post_conditions_met=False,
            rollback_required=False,
            reasons=reasons,
            error=str(exc),
        )

    # 5. 后置校验
    #
    # 没有可判定条件的计划在这里**必须**判成"没验成"，不能默认通过：
    # "执行了但看不出好没好"和"执行了并且好了"是两件事，混同等于默许
    # 任何没写后置条件的动作都自动成功。
    recovered = bool(verifier.verify(plan))
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


@dataclass
class LoopResult:
    """执行 + （必要时）回滚的完整结果。"""

    executed: ExecutionResult
    rollback: ExecutionResult | None = None
    notes: list[str] | None = None

    @property
    def settled(self) -> bool:
        """这次修复是否有了确定结局（成功，或已回滚）。"""
        if self.executed.status is ExecutionStatus.SUCCESS:
            return True
        return (self.rollback is not None
                and self.rollback.status in (ExecutionStatus.SUCCESS,
                                             ExecutionStatus.DRY_RUN))


def execute_with_rollback(
    plan: RemediationPlan,
    *,
    policy: PolicyDecision,
    approval: Approval | None,
    registry: ActionRegistry,
    executor,
    verifier,
    policy_engine: PolicyEngine,
    dry_run: bool = False,
    reason: str | None = None,
) -> LoopResult:
    """执行计划；后置条件不满足时按计划自己声明的回滚路线撤回去。

    ## 被替换的缺陷

    `rollback.py` 里 `build_rollback_plan` / `rollback_required_for` 早就写好了，
    却**没有任何调用方**：`execute_plan` 在 NOT_VERIFIED 时只是把
    `rollback_required=True` 挂到结果上，然后就没有下文了。
    于是"改坏了要撤回来"这条唯一的兜底只存在于类型里。

    ## 回滚的授权从哪来

    回滚计划是另一份计划（动作、参数都不同），但它是**原计划里写明的那一次**
    回滚，而 `content_hash` 包含 `rollback` 字段 —— 批准原计划时就看见了它。
    所以回滚沿用原审批，并把 `approval_for=plan` 传下去：验证的是原计划的哈希，
    而不是放宽"要不要审批"。
    """
    # 在函数内导入：`rollback` 需要本模块的 `ExecutionResult`，
    # 模块级互相导入会得到 partially initialized module。
    from traceops.remediation.rollback import RollbackUnavailable, build_rollback_plan

    notes: list[str] = []
    executed = execute_plan(plan, policy=policy, approval=approval, registry=registry,
                            executor=executor, verifier=verifier, dry_run=dry_run)
    if dry_run:
        notes.append("dry run: nothing was executed there is nothing to roll back")
        return LoopResult(executed=executed, notes=notes)
    if not executed.rollback_required:
        return LoopResult(executed=executed, notes=notes)

    try:
        rollback_plan = build_rollback_plan(
            plan, registry=registry,
            reason=reason or "post conditions were not met after execution")
    except RollbackUnavailable as exc:
        # 没有回滚路线不是"安静地算了"：这件事必须出现在结果里
        notes.append(f"no rollback was applied: {exc}")
        return LoopResult(executed=executed, notes=notes)

    # 回滚计划的策略要**重新评估**：它的风险级别可能比原计划更高
    # （撤不回来的动作不能因为"原来是 medium"就跟着免检）。
    rollback_policy = policy_engine.evaluate(rollback_plan, registry)
    if not rollback_policy.allowed:
        notes.append("rollback was denied by policy: "
                     + "; ".join(rollback_policy.reasons))
        return LoopResult(executed=executed, notes=notes)
    rollback = execute_plan(rollback_plan, policy=rollback_policy, approval=approval,
                            registry=registry, executor=executor, verifier=verifier,
                            dry_run=False, approval_for=plan)
    notes.append(f"rollback {rollback_plan.action} on {rollback_plan.target}: "
                 f"{rollback.status.value}")
    if not rollback_plan.post_conditions:
        notes.append("rollback could not be verified: no baseline was recorded "
                     "for this plan")
    return LoopResult(executed=executed, rollback=rollback, notes=notes)
