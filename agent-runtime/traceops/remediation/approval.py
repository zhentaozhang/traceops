"""审批记录：绑定计划内容哈希。

审批不绑定一句话（"同意修复 Redis"），而是绑定一份不可变的执行计划。
参数一变，原审批作废，必须重新审核。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from traceops.remediation.plan import RemediationPlan


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class PlanIntegrityError(Exception):
    """审批对应的计划与当前计划内容不一致（被改过）。"""


@dataclass(frozen=True)
class Approval:
    plan_id: str
    plan_hash: str
    approver: str
    decision: ApprovalDecision
    reason: str
    decided_at: datetime


def approve(
    plan: RemediationPlan,
    *,
    approver: str,
    decision: ApprovalDecision,
    reason: str,
) -> Approval:
    if not approver or not approver.strip():
        raise ValueError("approver is required")
    if not reason or not reason.strip():
        # 空理由的审批在复盘时无法解释，直接拒绝记录
        raise ValueError("approval reason is required")
    return Approval(
        plan_id=plan.plan_id,
        plan_hash=plan.content_hash(),
        approver=approver,
        decision=decision,
        reason=reason,
        decided_at=datetime.now(UTC),
    )


def verify_approval_matches(approval: Approval, plan: RemediationPlan) -> bool:
    """审批与计划必须同 id 且同内容哈希，否则抛 PlanIntegrityError。"""
    if approval.plan_id != plan.plan_id:
        raise PlanIntegrityError(
            f"approval is for plan {approval.plan_id!r}, not {plan.plan_id!r}"
        )
    if approval.plan_hash != plan.content_hash():
        raise PlanIntegrityError(
            f"approval content hash mismatch for {plan.plan_id!r}: "
            f"plan changed after approval was granted"
        )
    return True
