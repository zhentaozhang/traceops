"""受控修复的操作员入口。

## 为什么需要它

`remediation/` 里有一套完整的闸门（策略 → 审批 → 内容哈希 → 执行 → 复验 → 回滚），
但**没有任何入口**：`build_plan` / `execute_plan` 在仓库里只被自己调用。
控制面侧也没有审批接口 —— 于是"受控修复"只存在于类型定义里。

这个 CLI 是那条链路的入口，也是它唯一诚实的入口形态：**修复由操作员带外执行**。
控制面不代执行，因为它没有修复相关代码；而"操作员在终端里按三步走"本身就是
一次人工审批，比一个没有鉴权的 REST 端点更接近真实流程。

## 三步，三步之间靠磁盘上的审批库连接

    plan    构造计划 → 过策略 → 落一条审批请求（打印 request_id 与 plan_hash）
    approve 审批人决策（强制角色判定 + 职责分离 + 与主体一致）
    execute 执行（校验授权覆盖范围 → 执行 → 复验 → 不达标自动回滚）

`execute` 必须用**同样的参数**再构造一次计划，程序会比对 `content_hash` ——
参数变一点，原来的审批自动失效。这是"批了一个、执行另一个"唯一的防线。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from traceops.remediation.actions import ActionRegistry, register_builtin_actions
from traceops.remediation.approval import Approval, ApprovalDecision, approve
from traceops.remediation.approval_store import ApprovalRequest, ApprovalStore
from traceops.remediation.backends import (
    BackendUnavailable,
    RecordingExecutor,
    SubprocessExecutor,
    executor_from_config,
)
from traceops.remediation.executor import execute_with_rollback
from traceops.remediation.plan import RemediationPlan, build_plan
from traceops.remediation.policy import PolicyEngine
from traceops.remediation.postconditions import PostCondition
from traceops.remediation.verify import RecoveryVerifier, verify_recovery
from traceops.security.access import (
    AccessDenied,
    Action,
    Principal,
    Role,
    authorize,
)
from traceops.tools.adapters import DatasourceAdapters, PrometheusAdapter

DEFAULT_APPROVAL_DB = Path("state") / "approvals.db"


def _registry() -> ActionRegistry:
    return register_builtin_actions(ActionRegistry())


def _principal(name: str, role: str) -> Principal:
    return Principal(name=name, role=Role(role))


def _plan(args: argparse.Namespace) -> RemediationPlan:
    conditions: list[PostCondition | str] = []
    if args.error_rate_below is not None:
        conditions.append(PostCondition.error_rate_below(
            args.target, args.error_rate_below,
            window_seconds=args.verify_window_seconds))
    baseline = {}
    if args.baseline_error_rate is not None:
        baseline[args.target] = args.baseline_error_rate
    return build_plan(
        incident_id=args.incident,
        action=args.action,
        target=args.target,
        parameters=json.loads(args.parameters),
        rationale=args.reason,
        rollback=json.loads(args.rollback) if args.rollback else None,
        registry=_registry(),
        post_conditions=conditions or None,
        baseline=baseline,
    )


def _store(args: argparse.Namespace) -> ApprovalStore:
    path = Path(args.approval_db)
    if path is not DEFAULT_APPROVAL_DB and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    return ApprovalStore(path)


def _emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


# ---------------- 子命令 ----------------


def cmd_plan(args: argparse.Namespace) -> int:
    plan = _plan(args)
    decision = PolicyEngine().evaluate(plan, _registry())
    payload: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "plan_hash": plan.content_hash(),
        "action": plan.action,
        "target": plan.target,
        "parameters": plan.parameters,
        "post_conditions": [p.describe() if isinstance(p, PostCondition) else str(p)
                            for p in plan.post_conditions],
        "policy": {"allowed": decision.allowed, "requires_approval": decision.requires_approval,
                   "reasons": decision.reasons},
    }
    if not decision.allowed:
        payload["outcome"] = "blocked-by-policy"
        _emit(payload)
        return 2

    if decision.requires_approval:
        store = _store(args)
        request = store.request(ApprovalRequest.create(
            plan_id=plan.plan_id,
            plan_hash=plan.content_hash(),
            requested_by=args.as_name,
            action=plan.action,
            target=plan.target,
            reason=args.reason,
            ttl_seconds=args.ttl_seconds,
        ))
        payload["request_id"] = request.request_id
        payload["expires_at"] = request.expires_at.isoformat()
        payload["outcome"] = "awaiting-approval"
    else:
        payload["outcome"] = "no-approval-needed"
    _emit(payload)
    return 0


def cmd_decide(args: argparse.Namespace, *, decision: ApprovalDecision) -> int:
    store = _store(args)
    principal = _principal(args.as_name, args.role)
    store.decide(args.request, decision=decision, approver=args.as_name,
                 reason=args.reason, principal=principal)
    request = store.get(args.request)
    _emit({"request_id": args.request,
           "status": request.status.value if request else "missing",
           "decision": decision.value})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    request = store.get(args.request)
    if request is None:
        _emit({"request_id": args.request, "outcome": "not-found"})
        return 1
    _emit({"request_id": request.request_id,
           "status": request.status.value,
           "plan_hash": request.plan_hash,
           "action": request.action,
           "target": request.target,
           "has_live_mandate": request.has_live_mandate,
           "expires_at": request.expires_at.isoformat()})
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    plan = _plan(args)
    store = _store(args)
    request = store.get(args.request)
    if request is None:
        _emit({"outcome": "blocked", "reasons": [f"unknown approval request {args.request}"]})
        return 2
    if request.plan_hash != plan.content_hash():
        # 参数变了：原审批作废。这不是"提示"，是拒绝执行。
        _emit({"outcome": "blocked",
               "reasons": ["plan hash does not match the approved plan; "
                           "the parameters changed after approval"]})
        return 2

    try:
        store.require_mandate(args.request, plan_id=plan.plan_id,
                              plan_hash=plan.content_hash(),
                              action=plan.action, target=plan.target)
    except Exception as exc:  # MandateUnavailable / KeyError
        _emit({"outcome": "blocked", "reasons": [f"{type(exc).__name__}: {exc}"]})
        return 2

    # 授权判定：只有 EXECUTOR 角色能真的动手。这条和审批那一步是**两个**判定点，
    # 不能合并 —— 同一个人既批又执行时，两道闸至少各留一条记录。
    authorize(_principal(args.as_name, args.role), Action.EXECUTE_REMEDIATION)

    approval = approve(plan, approver=request.approver or args.as_name,
                       decision=ApprovalDecision.APPROVED,
                       reason=request.decision_reason or "approved")

    adapters = DatasourceAdapters(
        prometheus=PrometheusAdapter(args.prometheus_url) if args.prometheus_url else None)
    verifier = RecoveryVerifier(adapters)
    engine = PolicyEngine()

    if args.dry_run:
        executor = RecordingExecutor()
    else:
        try:
            executor = executor_from_config(args.executor_config)
        except BackendUnavailable as exc:
            _emit({"outcome": "blocked", "reasons": [str(exc)]})
            return 2

    loop = execute_with_rollback(
        plan,
        policy=engine.evaluate(plan, _registry()),
        approval=approval,
        registry=_registry(),
        executor=executor,
        verifier=verifier,
        policy_engine=engine,
        dry_run=args.dry_run,
        reason=args.reason,
    )
    outcome = verifier.last_outcome
    _emit({
        "outcome": loop.executed.status.value,
        "settled": loop.settled,
        "dry_run": args.dry_run,
        "executed": {"status": loop.executed.status.value, "reasons": loop.executed.reasons},
        "verification": outcome.to_dict() if outcome is not None else None,
        "rollback": None if loop.rollback is None else {
            "status": loop.rollback.status.value, "reasons": loop.rollback.reasons},
        "notes": loop.notes,
    })
    return 0 if loop.executed.status.value in ("success", "dry_run") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traceops-remediate",
        description="受控修复：构造计划、人工审批、执行并复验（不达标自动回滚）")
    parser.add_argument("--approval-db", default=str(DEFAULT_APPROVAL_DB),
                        help="审批库路径（默认 state/approvals.db）")
    sub = parser.add_subparsers(dest="command", required=True)

    def plan_args(target: argparse.ArgumentParser, *, the_principal: bool) -> None:
        target.add_argument("--incident", required=True)
        target.add_argument("--action", required=True, choices=_registry().names())
        target.add_argument("--target", required=True)
        target.add_argument("--parameters", default="{}")
        target.add_argument("--rollback", default=None,
                            help='形如 {"action": "scale_replicas", "parameters": {"replicas": 2}}')
        target.add_argument("--reason", required=True)
        target.add_argument("--error-rate-below", type=float, default=None,
                            help="执行后要观测到的错误率上限；不给则无法自动复验")
        target.add_argument("--baseline-error-rate", type=float, default=None,
                            help="执行前的错误率，用来区分'没修好'和'修坏了'")
        target.add_argument("--verify-window-seconds", type=int, default=300)
        if the_principal:
            target.add_argument("--as", dest="as_name", required=True, help="操作员身份")
            target.add_argument("--role", default="OPERATOR", choices=[r.value for r in Role])

    plan_cmd = sub.add_parser("plan", help="构造计划并落审批请求")
    plan_args(plan_cmd, the_principal=True)
    plan_cmd.add_argument("--ttl-seconds", type=float, default=3600)
    plan_cmd.set_defaults(func=cmd_plan)

    approve_cmd = sub.add_parser("approve", help="批准（需要 APPROVER 角色）")
    approve_cmd.add_argument("--request", required=True)
    approve_cmd.add_argument("--as", dest="as_name", required=True)
    approve_cmd.add_argument("--role", default="APPROVER", choices=[r.value for r in Role])
    approve_cmd.add_argument("--reason", required=True)
    approve_cmd.set_defaults(func=lambda a: cmd_decide(a, decision=ApprovalDecision.APPROVED))

    reject_cmd = sub.add_parser("reject", help="拒绝")
    reject_cmd.add_argument("--request", required=True)
    reject_cmd.add_argument("--as", dest="as_name", required=True)
    reject_cmd.add_argument("--role", default="APPROVER", choices=[r.value for r in Role])
    reject_cmd.add_argument("--reason", required=True)
    reject_cmd.set_defaults(func=lambda a: cmd_decide(a, decision=ApprovalDecision.REJECTED))

    status_cmd = sub.add_parser("status", help="查看审批请求")
    status_cmd.add_argument("--request", required=True)
    status_cmd.set_defaults(func=cmd_status)

    execute_cmd = sub.add_parser("execute", help="执行已批准的计划")
    plan_args(execute_cmd, the_principal=False)
    execute_cmd.add_argument("--request", required=True)
    execute_cmd.add_argument("--as", dest="as_name", default="executor",
                             help="执行者身份（写入审计）")
    execute_cmd.add_argument("--role", default="EXECUTOR", choices=[r.value for r in Role])
    execute_cmd.add_argument("--dry-run", action="store_true", default=False)
    execute_cmd.add_argument("--prometheus-url", default=None,
                             help="给复验用的 Prometheus 地址；不给则无法自动复验")
    execute_cmd.add_argument("--executor-config", default=None,
                             help="动作 → argv 的 JSON 配置；不给则拒绝真实写操作")
    execute_cmd.set_defaults(func=cmd_execute)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except AccessDenied as exc:
        _emit({"outcome": "denied", "reasons": [str(exc)]})
        return 3
    except (ValueError, KeyError) as exc:
        _emit({"outcome": "error", "reasons": [f"{type(exc).__name__}: {exc}"]})
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
