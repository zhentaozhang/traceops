"""执行平面闭环：执行 → 重新观测 → 不达标就按计划里写明的回滚撤回去。

## 被替换的缺陷

三件事各自独立地"存在但不工作"：

1. `post_conditions` 是自由文本（`["error_rate_below_threshold"]`），没人能执行它；
   `execute_plan` 的判据只是"这个列表非空"，于是"有注释"被当成"可验证"。
2. `execute_plan` 的 `verifier` 参数至今没有任何具体实现 —— 每一步"验证"都是
   假的。
3. `rollback.py` 的 `build_rollback_plan` / `rollback_required_for` **没有调用方**：
   `execute_plan` 只把 `rollback_required=True` 挂到结果上，然后什么也不做。

于是"改坏了要撤回来"这条唯一的兜底只活在类型里。

## 这一版守住的四条

- 条件是可判定的数据（种类/对象/阈值/窗口），不是注释；
- **不认识就是不可验证**：自由文本、`manual_check`、数据源连不上、窗口内没数据，
  全部落到 UNCHANGED 并写清原因，绝不默认通过；
- 有基线才能说 WORSE：只看阈值分不清"没修好"和"修坏了"；
- 回滚沿用**原审批**（`content_hash` 含 `rollback`，批准原计划即批准它），
  但回滚计划的策略要重新评估 —— 撤不回来的动作不能因为"原来是 medium"就免检。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.remediation.actions import (
    ActionRegistry,
    ActionSpec,
    RiskLevel,
    action_registry,
    register_builtin_actions,
)
from traceops.remediation.approval import ApprovalDecision, approve
from traceops.remediation.executor import (
    ExecutionStatus,
    execute_with_rollback,
)
from traceops.remediation.plan import build_plan
from traceops.remediation.policy import PolicyEngine
from traceops.remediation.postconditions import (
    ERROR_RATE_BELOW,
    MANUAL_CHECK,
    PostCondition,
    as_condition,
)
from traceops.remediation.verify import (
    Outcome,
    RecoveryVerifier,
    VerifyStatus,
    verify_recovery,
)

NOW = datetime(2026, 10, 30, 10, 10, 0, tzinfo=UTC)


class RecordingExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def execute(self, action: str, target: str, parameters: dict) -> dict:
        self.calls.append({"action": action, "target": target, "parameters": parameters})
        if self.fail:
            raise RuntimeError("executor rejected the action")
        return {"applied": True, "target": target}


class ScriptedProbes:
    """按调用顺序返回脚本化的错误率载荷。"""

    def __init__(self, *values: float | Exception | None) -> None:
        self._values = list(values)
        self.queries: list[tuple[str, str, str]] = []

    def query_service_error_rate(self, service, start, end):
        self.queries.append((service, start, end))
        value = self._values.pop(0)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return {"status": "success", "data": {"resultType": "matrix", "result": []}}
        return {"status": "success",
                "data": {"resultType": "matrix", "result": [
                    {"metric": {}, "values": [[1700000000, str(value)]]}]}}


def _registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="restart_test_service",
        description="重启测试环境服务",
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        allowed_targets={"order-service"},
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"grace_seconds": {"type": "integer", "minimum": 0, "maximum": 60}},
        },
    ))
    registry.register(ActionSpec(
        name="rollback_test_service",
        description="把测试环境服务恢复到改动前的状态",
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        allowed_targets={"order-service"},
        parameter_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"grace_seconds": {"type": "integer", "minimum": 0, "maximum": 60}},
        },
    ))
    return registry


def _plan(*, action: str = "restart_test_service", target: str = "order-service",
          rollback: dict | None = None,
          post_conditions=None, baseline: dict[str, float] | None = None):
    return build_plan(
        incident_id="inc-1",
        action=action,
        target=target,
        parameters={"grace_seconds": 5},
        rationale="error rate is climbing after the deploy",
        rollback=({"action": "restart_test_service", "parameters": {"grace_seconds": 0}}
                  if rollback is None else rollback),
        registry=_registry(),
        post_conditions=post_conditions,
        baseline=baseline,
    )


# ---------- 1. 条件是可判定的数据 ----------

class TestPostConditionsAreData:
    def test_a_threshold_condition_carries_its_target_and_limit(self):
        condition = PostCondition.error_rate_below("order-service", 0.05, window_seconds=120)

        assert condition.kind == ERROR_RATE_BELOW
        assert condition.to_dict() == {"kind": "error_rate_below", "service": "order-service",
                                       "threshold": 0.05, "window_seconds": 120}

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="unknown post-condition kind"):
            PostCondition(kind="service_healthy")

    def test_a_threshold_condition_needs_a_service_and_a_threshold(self):
        with pytest.raises(ValueError, match="requires a service"):
            PostCondition(kind=ERROR_RATE_BELOW, threshold=0.05)
        with pytest.raises(ValueError, match="non-negative threshold"):
            PostCondition(kind=ERROR_RATE_BELOW, service="order-service")

    def test_a_manual_check_must_say_what_a_person_has_to_confirm(self):
        with pytest.raises(ValueError, match="requires a note"):
            PostCondition(kind=MANUAL_CHECK)

    def test_free_text_is_not_silently_promoted_to_a_condition(self):
        # 自由文本不是"无需验证"，是"无法验证"
        assert as_condition("error_rate_below_threshold") is None
        assert as_condition({"kind": "error_rate_below", "service": "a",
                             "threshold": 0.1}).kind == ERROR_RATE_BELOW


# ---------- 2. 三层结论 ----------

class TestVerificationHasThreeOutcomes:
    def _verify(self, plan, probes, **kwargs):
        return verify_recovery(plan, adapters=probes, now=NOW, **kwargs)

    def test_a_met_threshold_is_recovery(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)])

        outcome = self._verify(plan, ScriptedProbes(0.01))

        assert outcome.status is VerifyStatus.RECOVERED
        assert outcome.checks[0]["met"] is True

    def test_missing_the_threshold_is_not_recovery(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)])

        outcome = self._verify(plan, ScriptedProbes(0.2))

        assert outcome.status is VerifyStatus.UNCHANGED
        assert "threshold 0.05" in outcome.reasons[0]

    def test_rising_above_the_baseline_is_worse(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)],
                     baseline={"order-service": 0.1})

        outcome = self._verify(plan, ScriptedProbes(0.3))

        # 0.3 既没达标、也比执行前的 0.1 更糟 —— 这就是"修坏了"，不是"还没好"
        assert outcome.status is VerifyStatus.WORSE
        assert "rose from 0.1 to 0.3" in outcome.reasons[0]

    def test_an_unreachable_probe_is_not_recovery(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)])

        outcome = self._verify(plan, ScriptedProbes(ConnectionError("prometheus down")))

        assert outcome.status is VerifyStatus.UNCHANGED
        assert "cannot verify automatically" in outcome.reasons[0]

    def test_an_empty_result_is_not_recovery(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)])

        outcome = self._verify(plan, ScriptedProbes(None))

        # 查到了但没有数据点：不能把"没测到"当"好了"
        assert outcome.status is VerifyStatus.UNCHANGED
        assert "no data points" in outcome.reasons[0]

    def test_a_plan_without_conditions_reports_cannot_verify(self):
        outcome = self._verify(_plan(), ScriptedProbes())

        # 老实现里这条路径直接 SUCCESS（`if plan.post_conditions` 为假即视为通过）
        assert outcome.status is VerifyStatus.UNCHANGED
        assert "declares no post-conditions" in outcome.reasons[0]

    def test_a_manual_check_blocks_automatic_verification(self):
        plan = _plan(post_conditions=[PostCondition.manual_check("confirm the pods are Ready")])

        outcome = self._verify(plan, ScriptedProbes())

        assert outcome.status is VerifyStatus.UNCHANGED
        assert "manual check" in outcome.reasons[0]

    def test_only_recovered_counts_as_verified(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)])

        verifier = RecoveryVerifier(ScriptedProbes(0.01), now=NOW)
        assert verifier.verify(plan) is True
        assert verifier.last_outcome.status is VerifyStatus.RECOVERED

        verifier = RecoveryVerifier(ScriptedProbes(0.9), now=NOW)
        assert verifier.verify(plan) is False
        # 没有基线就分不清"没修好"和"修坏了"，只能给 UNCHANGED
        assert verifier.last_outcome.status is VerifyStatus.UNCHANGED


# ---------- 3. 内置动作：显式注册，带审批与 target 形状 ----------

class TestBuiltinActions:
    def test_the_default_registry_is_still_empty(self):
        # 预置写操作等于"默认就能改环境"
        assert action_registry().names() == []

    def test_builtin_actions_must_be_registered_explicitly(self):
        registry = register_builtin_actions(ActionRegistry())

        assert registry.names() == ["restart_component", "scale_replicas"]

    def test_both_builtin_actions_need_an_approval(self):
        registry = register_builtin_actions(ActionRegistry())

        assert all(registry.get(name).requires_approval
                   for name in ("scale_replicas", "restart_component"))

    def test_a_well_formed_service_name_is_an_acceptable_target(self):
        registry = register_builtin_actions(ActionRegistry())

        plan = build_plan(incident_id="inc-1", action="scale_replicas",
                          target="order-service", parameters={"replicas": 4},
                          rationale="shed load", rollback={"replicas": 2}, registry=registry)

        assert plan.target == "order-service"
        assert plan.requires_approval is True

    def test_a_target_that_is_not_a_service_name_is_refused(self):
        registry = register_builtin_actions(ActionRegistry())

        with pytest.raises(ValueError, match="does not match the allowed pattern"):
            build_plan(incident_id="inc-1", action="scale_replicas",
                       target="kubectl delete ns prod", parameters={"replicas": 4},
                       rationale="x", rollback=None, registry=registry)

    def test_the_parameter_schema_is_enforced(self):
        registry = register_builtin_actions(ActionRegistry())

        with pytest.raises(ValueError, match="invalid parameters"):
            build_plan(incident_id="inc-1", action="scale_replicas", target="order-service",
                       parameters={"replicas": 0}, rationale="x", rollback=None,
                       registry=registry)


# ---------- 4. 闭环：不达标就回滚 ----------

class TestTheLoopCloses:
    def _approval(self, plan):
        return approve(plan, approver="sre-oncall", decision=ApprovalDecision.APPROVED,
                       reason="approved for the recovery test")

    def _run(self, plan, probes, *, policy_engine=None, approval=None, dry_run=False):
        registry = _registry()
        engine = policy_engine or PolicyEngine()
        return execute_with_rollback(
            plan,
            policy=engine.evaluate(plan, registry),
            approval=self._approval(plan) if approval is None else approval,
            registry=registry,
            executor=RecordingExecutor(),
            verifier=RecoveryVerifier(probes, now=NOW),
            policy_engine=engine,
            dry_run=dry_run,
        )

    def test_a_recovered_execution_needs_no_rollback(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)])

        loop = self._run(plan, ScriptedProbes(0.01))

        assert loop.executed.status is ExecutionStatus.SUCCESS
        assert loop.rollback is None
        assert loop.settled is True

    def test_a_failed_verification_triggers_the_declared_rollback(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)],
                     baseline={"order-service": 0.1})

        loop = self._run(plan, ScriptedProbes(0.4, 0.05))

        assert loop.executed.status is ExecutionStatus.NOT_VERIFIED
        assert loop.rollback is not None
        # 回滚动作是计划里写明的那个（把参数写回原值），不是凭空发明的
        assert loop.rollback.status in (ExecutionStatus.SUCCESS, ExecutionStatus.NOT_VERIFIED)
        assert any("rollback" in note for note in loop.notes)

    def test_the_original_approval_authorises_the_declared_rollback(self):
        # content_hash 包含 rollback，批准原计划就等于批准了它写明的那次回滚
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)],
                     baseline={"order-service": 0.1})

        loop = self._run(plan, ScriptedProbes(0.4, 0.05))

        assert loop.rollback is not None
        assert loop.rollback.status is ExecutionStatus.SUCCESS
        assert not any("approval" in reason for reason in loop.rollback.reasons)

    def test_a_plan_without_a_rollback_route_says_so(self):
        plan = build_plan(incident_id="inc-1", action="restart_test_service",
                          target="order-service", parameters={"grace_seconds": 5},
                          rationale="x", rollback=None, registry=_registry(),
                          post_conditions=[PostCondition.error_rate_below("order-service", 0.05)])

        loop = self._run(plan, ScriptedProbes(0.4))

        assert loop.rollback is None
        assert any("no rollback" in note for note in loop.notes)
        assert loop.settled is False

    def test_a_policy_that_forbids_the_rollback_stops_it(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)],
                     rollback={"action": "rollback_test_service",
                               "parameters": {"grace_seconds": 0}})
        engine = PolicyEngine(denied_actions={"rollback_test_service"})

        loop = self._run(plan, ScriptedProbes(0.4), policy_engine=engine)

        # 回滚也是一个动作，也要过策略 —— 不能用"这是撤回"当豁免
        assert loop.rollback is None
        assert any("denied by policy" in note for note in loop.notes)

    def test_a_dry_run_never_rolls_anything_back(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)])

        loop = self._run(plan, ScriptedProbes(0.4), dry_run=True)

        assert loop.executed.status is ExecutionStatus.DRY_RUN
        assert loop.rollback is None
        assert any("dry run" in note for note in loop.notes)

    def test_an_approval_for_another_plan_still_blocks_the_rollback(self):
        plan = _plan(post_conditions=[PostCondition.error_rate_below("order-service", 0.05)])
        # 参数不同 -> content_hash 不同 -> 审批对不上
        other = build_plan(incident_id="inc-1", action="restart_test_service",
                           target="order-service", parameters={"grace_seconds": 30},
                           rationale="a different change", rollback={"action": "restart_test_service",
                                                                    "parameters": {"grace_seconds": 0}},
                           registry=_registry())

        approval = self._approval(other)
        loop = self._run(plan, ScriptedProbes(0.4), approval=approval)

        # 主执行就被拒（审批对不上），根本走不到回滚
        assert loop.executed.status is ExecutionStatus.BLOCKED
        assert loop.rollback is None
