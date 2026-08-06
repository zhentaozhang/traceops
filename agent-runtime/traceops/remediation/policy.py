"""Policy Engine：先判合法性，再谈审批（REVISIONS-1.1 R7 / 蓝皮书 §9.2）。

顺序不可颠倒：
    policy.evaluate() → 若不合法，直接拒绝（无论谁来审批都不行）
                      → 若合法但需审批，才进入人工审批

理由：人工审批是一种**授权**，不是**豁免**。管理员再有权，也不能把
"策略明确禁止的操作"变成合法操作。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from traceops.remediation.actions import ActionRegistry, RiskLevel
from traceops.remediation.plan import RemediationPlan


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reasons: list[str] = field(default_factory=list)


class PolicyEngine:
    def __init__(
        self,
        *,
        denied_actions: set[str] | None = None,
        denied_risks: set[RiskLevel] | None = None,
        max_risk: RiskLevel = RiskLevel.HIGH,
    ) -> None:
        self.denied_actions = denied_actions or set()
        self.denied_risks = denied_risks or set()
        self.max_risk = max_risk

    def evaluate(self, plan: RemediationPlan, registry: ActionRegistry) -> PolicyDecision:
        reasons: list[str] = []
        allowed = True

        # 动作必须在注册表里（build_plan 已保证，这里防御性再查一次）
        if plan.action not in registry.names():
            return PolicyDecision(False, True, [f"action {plan.action!r} is not registered"])

        spec = registry.get(plan.action)

        if plan.action in self.denied_actions:
            allowed = False
            reasons.append(f"action {plan.action!r} is explicitly denied by policy")

        if plan.risk in self.denied_risks:
            allowed = False
            reasons.append(f"risk level {plan.risk.value!r} is denied by policy")

        if plan.risk.order > self.max_risk.order:
            allowed = False
            reasons.append(
                f"risk level {plan.risk.value!r} exceeds max allowed {self.max_risk.value!r}"
            )

        # target 白名单在 build_plan 校验过；这里防的是"绕过 build_plan 手工构造 plan"
        if plan.target not in spec.allowed_targets:
            allowed = False
            reasons.append(f"target {plan.target!r} not allowed for action {plan.action!r}")

        requires_approval = spec.requires_approval or plan.risk is RiskLevel.HIGH

        if allowed and not reasons:
            reasons.append(f"policy passed for {plan.action!r} (risk={plan.risk.value})")

        return PolicyDecision(allowed=allowed, requires_approval=requires_approval, reasons=reasons)
