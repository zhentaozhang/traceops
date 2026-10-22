"""回滚：从原计划派生一个**可提交审批**的反向计划。

## 为什么不是"自动回滚"

蓝图 §9.4 与 `executor.py` 的注释都明确了立场：

> 执行成功 ≠ 业务恢复。……**不自动回滚**：回滚本身是另一个有风险的动作，
> 必须重新走同样流程。

这个立场是对的 —— 代码自作主张地改环境，正是本项目要防的事。
但它留下一个空洞：`RemediationPlan.rollback` 字段**存在却从未被使用**。
`rollback_required=True` 只是一句标记，运维拿到它之后还得自己想办法。

## 本模块补的是什么

把"退回去该做什么"变成**流程能直接用**的东西：

- `build_rollback_plan(original, ...)` → 一个独立 `RemediationPlan`
- 它有自己的 `plan_id` 与 `content_hash()`（不能被原审批覆盖）
- 它必须走**同一套**策略 / 审批 / 哈希闸门（`execute_plan` 原样适用）
- 原计划没写 `rollback` 时抛 `RollbackUnavailable`，**不编造**

于是 `rollback_required` 不再是空话：运维拿到的是一个可以直接提交审批的
回滚计划，而不是"自己临时想怎么退"。
"""
from __future__ import annotations

from dataclasses import replace

from traceops.remediation.actions import ActionRegistry, RiskLevel
from traceops.remediation.executor import ExecutionResult
from traceops.remediation.plan import RemediationPlan


class RollbackUnavailable(Exception):
    """原计划没有声明回滚方案。

    宁可报错也不要编一个：凭空构造"反向操作"可能把系统改得更糟
    （例如"把副本数从 3 改回 1"在故障期间是危险的）。
    """


def build_rollback_plan(
    original: RemediationPlan,
    *,
    registry: ActionRegistry,
    reason: str,
) -> RemediationPlan:
    """从原计划派生回滚计划。

    Args:
        original: 已执行（且通常 post-condition 未满足）的计划。
        registry: 用于校验回滚动作确实已注册（不能回滚到未注册的动作）。
        reason: 触发回滚的原因，会写进 rationale 以便审计追溯。

    Raises:
        RollbackUnavailable: 原计划没有 `rollback` 声明，或声明的动作未注册。

    设计要点：
    - **不继承原计划的后置条件**：回滚的目标是"恢复到可观测状态"，
      沿用原后置条件会让回滚永远判 NOT_VERIFIED（原动作本就没成功）。
      因此用空列表 —— 由外部验证器判断，而不是假装继承。
    - **保留原风险级别**：回滚动作的风险不低于原动作（改回小池子同样可能
      造成容量问题），压低风险等级等于偷偷放宽闸门。
    """
    if not original.rollback:
        raise RollbackUnavailable(
            f"plan {original.plan_id} declares no rollback; "
            "refusing to invent one - high-risk actions must ship with a rollback"
        )

    rollback_spec = original.rollback
    action = str(rollback_spec.get("action") or "").strip()
    if not action:
        raise RollbackUnavailable(
            f"plan {original.plan_id} rollback has no action name")

    try:
        spec = registry.get(action)
    except KeyError as exc:
        raise RollbackUnavailable(
            f"rollback action {action!r} is not registered; "
            "cannot roll back to an unregistered action"
        ) from exc

    parameters = dict(rollback_spec.get("parameters") or {})
    target = str(rollback_spec.get("target") or original.target)

    return RemediationPlan(
        plan_id=f"rollback-{original.plan_id}",
        incident_id=original.incident_id,
        action=action,
        target=target,
        parameters=parameters,
        # 风险等级不降：回滚同样是写操作
        risk=max(original.risk, spec.risk, key=lambda r: r.order),
        requires_approval=spec.requires_approval,
        rationale=(
            f"rollback of plan {original.plan_id} ({original.action} on {original.target}); "
            f"reason: {reason}"
        ),
        rollback=_inverse_of_original(original),
        # 刻意**不继承**原后置条件（见 docstring），改用"回到基线"这一条：
        # 原动作本就没成功，沿用它的条件会让回滚永远判 NOT_VERIFIED。
        post_conditions=_rollback_conditions(original),
        baseline=dict(original.baseline or {}),
    )


def _rollback_conditions(original: RemediationPlan) -> list:
    """回滚的后置条件 = "回到执行前的基线"。

    没有基线就不编条件：回滚会被判成"无法自动验证"，这比假装通过诚实。
    容差 10%：正常抖动会让"严格低于基线"永远不成立，于是每次回滚都被判成
    NOT_VERIFIED，而那个状态的用途是"该找人看"，不是"每次都找人看"。
    """
    from traceops.remediation.postconditions import PostCondition

    return [
        PostCondition.error_rate_below(service, value * 1.1)
        for service, value in sorted((original.baseline or {}).items())
    ]


def _inverse_of_original(original: RemediationPlan) -> dict:
    """回滚计划的 rollback = 回到原计划（使回滚本身也可回滚）。"""
    return {
        "action": original.action,
        "target": original.target,
        "parameters": dict(original.parameters),
    }


def rollback_required_for(result: ExecutionResult) -> bool:
    """这次执行结果是否要求回滚。

    单独成一个函数而不是让调用方读字段：字段会被误读成"建议"，
    而这里的语义是"当前环境可能处于比执行前更糟的状态"。
    """
    return bool(result.rollback_required)
