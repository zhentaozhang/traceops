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
from traceops.remediation.postconditions import PostCondition


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
    #: 执行后要观测到的条件。允许 `PostCondition`（可判定）与自由文本
    #: （不可判定 —— `verify_recovery` 会把它记成"无法自动验证"，不是"通过"）。
    post_conditions: list[PostCondition | str] = field(default_factory=list)
    #: 构造计划时观测到的基线值（如当时的错误率）。**不进 `content_hash`**：
    #: 它影响的是"执行后算不算变坏"，不影响"执行什么"，所以基线更新
    #: 不应该让一份已经批准的审批失效。
    baseline: dict[str, float] = field(default_factory=dict)

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
    post_conditions: list[PostCondition | str] | None = None,
    baseline: dict[str, float] | None = None,
) -> RemediationPlan:
    """构造计划并立刻校验：动作已注册、target 在白名单、参数符合 schema。

    校验失败抛 ValueError —— 非法计划不应该进入审批流程。
    """
    spec = registry.get(action)  # 未注册动作在这里就 KeyError

    if not spec.accepts_target(target):
        raise ValueError(
            f"target {target!r} is not allowed for action {action!r} "
            f"(allowed targets: {sorted(spec.allowed_targets) or 'none'}, "
            f"pattern: {spec.allowed_target_pattern})"
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
        post_conditions=list(post_conditions) if post_conditions is not None
        else list(spec.post_conditions),
        baseline=dict(baseline or {}),
    )
