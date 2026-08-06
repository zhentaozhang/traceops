"""修复计划：不可变、内容寻址。

审批绑定的是 plan 的内容哈希 —— 参数一变，原审批自动作废。
这是防止"批了一个操作、执行另一个操作"唯一可靠的机制。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from traceops.remediation.actions import ActionRegistry, RiskLevel


@dataclass(frozen=True)
class RemediationPlan:
    plan_id: str
    incident_id: str
    action: str
    target: str
    parameters: dict[str, Any]
    risk: RiskLevel
    requires_approval: bool
    rationale: str
    rollback: dict[str, Any] | None
    post_conditions: list[str] = field(default_factory=list)

    def content_hash(self) -> str:
        """对影响执行结果的字段做规范 JSON 哈希（不含 plan_id，便于重放比对）。"""
        canonical = json.dumps(
            {
                "incident_id": self.incident_id,
                "action": self.action,
                "target": self.target,
                "parameters": self.parameters,
                "rollback": self.rollback,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_plan(
    *,
    incident_id: str,
    action: str,
    target: str,
    parameters: dict[str, Any],
    rationale: str,
    rollback: dict[str, Any] | None,
    registry: ActionRegistry,
    plan_id: str | None = None,
) -> RemediationPlan:
    """构造计划并立刻校验：动作已注册、target 在白名单、参数符合 schema。

    校验失败抛 ValueError —— 非法计划不应该进入审批流程。
    """
    spec = registry.get(action)  # 未注册动作在这里就 KeyError

    if target not in spec.allowed_targets:
        raise ValueError(
            f"target {target!r} is not allowed for action {action!r} "
            f"(allowed: {sorted(spec.allowed_targets)})"
        )

    registry.validate_parameters(action, parameters)

    return RemediationPlan(
        plan_id=plan_id or f"plan-{hashlib.sha256(f'{incident_id}:{action}:{target}'.encode()).hexdigest()[:12]}",
        incident_id=incident_id,
        action=action,
        target=target,
        parameters=parameters,
        risk=spec.risk,
        requires_approval=spec.requires_approval,
        rationale=rationale,
        rollback=rollback,
        post_conditions=list(spec.post_conditions),
    )
