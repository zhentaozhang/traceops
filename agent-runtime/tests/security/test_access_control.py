"""用户 / 角色 / 权限：权限必须服务于**真实的安全边界**。

## 为什么不能做成通用 RBAC（否则就是纯外壳）

做成一个抽象的角色表、然后声称"权限已实现"，是最容易的自欺 ——
它不改变任何真实行为。

本项目真正需要权限的地方只有四处，且都很具体：

| 动作 | 风险 | 谁该能做 |
|---|---|---|
| 读事故 / 结论 / 证据 | 低 | 所有角色 |
| 人工创建事故 | 低-中 | operator 及以上 |
| **审批修复计划** | **高**：批准后真的会改环境 | **approver** |
| 执行已批准的计划 | 高 | **executor**（且必须已有有效审批） |

所以本模块的核心是**审批权与执行权的分离**，不是"用户管理"。

## 三条必须成立的约束

1. **审批权与执行权分离**：批准者不能执行，执行者不能批准 ——
   否则"复核"只是同一个人点两次
2. **权限不足必须拒绝，不是降级**：不能"你没权限，所以我用只读模式跑" ——
   静默降级会让调用方以为动作成功了
3. **审计可追溯**：`ApprovalStore` 存了 approver 字符串，
   但身份必须能追到角色，否则审计只看到一个孤立的用户名

## 边界（诚实）

- **不做认证**：密码/会话/JWT 属于部署层（OIDC/SSO）。本模块只做**授权判定**
- **不持久化用户表**：身份由调用方传入
- **不改变现有审批流**：它给 `ApprovalStore` 增加一个前置判定，不替换既有逻辑
"""
from __future__ import annotations

import pytest

from traceops.security.access import (
    AccessDenied,
    Action,
    Principal,
    Role,
    authorize,
    can_approve,
    require_separation_of_duty,
)


class TestRolesExist:
    def test_roles_are_closed_set(self):
        assert {r.name for r in Role} == {"VIEWER", "OPERATOR", "APPROVER", "EXECUTOR"}

    def test_role_permissions_are_declared(self):
        for role in Role:
            assert role.permissions, f"{role.name} must declare what it can do"


class TestReadIsOpenToAllRoles:
    def test_viewer_can_read_incidents(self):
        authorize(Principal("alice", Role.VIEWER), Action.READ_INCIDENT)

    def test_viewer_cannot_create_incident(self):
        with pytest.raises(AccessDenied, match="create_incident"):
            authorize(Principal("alice", Role.VIEWER), Action.CREATE_INCIDENT)

    def test_operator_can_create_incident(self):
        authorize(Principal("bob", Role.OPERATOR), Action.CREATE_INCIDENT)


class TestApprovalRightIsGuarded:
    def test_operator_cannot_approve_remediation(self):
        """审批是**写环境**的前置，不能和普通操作同级。"""
        with pytest.raises(AccessDenied):
            authorize(Principal("bob", Role.OPERATOR), Action.APPROVE_REMEDIATION)

    def test_approver_can_approve(self):
        authorize(Principal("carol", Role.APPROVER), Action.APPROVE_REMEDIATION)

    def test_viewer_cannot_approve(self):
        with pytest.raises(AccessDenied):
            authorize(Principal("alice", Role.VIEWER), Action.APPROVE_REMEDIATION)

    def test_operator_cannot_execute_remediation(self):
        with pytest.raises(AccessDenied):
            authorize(Principal("bob", Role.OPERATOR), Action.EXECUTE_REMEDIATION)


class TestSeparationOfDuty:
    """申请人与审批人必须是**不同的人** —— 这是审批的意义所在。"""

    def test_same_person_cannot_approve_own_request(self):
        with pytest.raises(AccessDenied, match="separation"):
            require_separation_of_duty(requested_by="bob", approver="bob")

    def test_different_people_are_fine(self):
        require_separation_of_duty(requested_by="bob", approver="carol")

    def test_cannot_execute_what_you_approved(self):
        """批准者不能自己执行 —— 否则复核形同虚设。"""
        principal = Principal("carol", Role.APPROVER)
        with pytest.raises(AccessDenied):
            authorize(principal, Action.EXECUTE_REMEDIATION)

    def test_executor_cannot_approve(self):
        with pytest.raises(AccessDenied):
            authorize(Principal("dave", Role.EXECUTOR), Action.APPROVE_REMEDIATION)


class TestDenialIsExplicitNotSilentDowngrade:
    def test_denial_names_the_action_and_role(self):
        """拒绝必须说清"为什么"，不能只是抛个空泛的 Forbidden。"""
        with pytest.raises(AccessDenied) as exc:
            authorize(Principal("alice", Role.VIEWER), Action.APPROVE_REMEDIATION)
        message = str(exc.value).lower()
        assert "viewer" in message
        assert "approve_remediation" in message

    def test_unknown_action_is_rejected_not_allowed(self):
        """未知动作必须**拒绝**（fail-closed）—— 不能因为"没规则"就放行。"""
        with pytest.raises(AccessDenied):
            authorize(Principal("alice", Role.VIEWER), "totally_new_action")


class TestAuditTrailCarriesIdentity:
    def test_principal_exposes_auditable_identity(self):
        principal = Principal("carol", Role.APPROVER)
        assert principal.identity() == "carol:APPROVER"


class TestAuthorizationIsActuallyEnforcedInTheApprovalPath:
    """权限必须**接进真实路径** —— 否则就是又一处"完整实现但从不被调用"。

    这正是前几轮反复出现的病症（`AlertCorrelation` 12 个测试、生产零引用）。
    一条只被自己的测试调用的权限模块，安全价值是零。
    """

    def test_approval_store_rejects_an_unprivileged_approver(self, tmp_path):
        from traceops.remediation.approval import ApprovalDecision
        from traceops.remediation.approval_store import ApprovalRequest, ApprovalStore

        store = ApprovalStore(tmp_path / "a.db", default_ttl_seconds=600)
        request = store.request(ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="bob",
            action="scale_pool", target="redis-pool", reason="pool exhausted"))

        # operator 没有审批权 → 必须拒绝，不是静默放过
        with pytest.raises(AccessDenied):
            store.decide(request.request_id, decision=ApprovalDecision.APPROVED,
                         approver="bob-operator",
                         reason="looks fine",
                         principal=Principal("bob-operator", Role.OPERATOR))

    def test_approval_store_accepts_a_privileged_approver(self, tmp_path):
        from traceops.remediation.approval import ApprovalDecision
        from traceops.remediation.approval_store import ApprovalRequest, ApprovalStore

        store = ApprovalStore(tmp_path / "a.db", default_ttl_seconds=600)
        request = store.request(ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="bob",
            action="scale_pool", target="redis-pool", reason="pool exhausted"))

        decided = store.decide(request.request_id, decision=ApprovalDecision.APPROVED,
                               approver="carol", reason="restoring pool is correct",
                               principal=Principal("carol", Role.APPROVER))
        assert decided.status.value == "APPROVED"

    def test_self_approval_is_refused_even_with_the_right_role(self, tmp_path):
        """即使是 approver，也不能批自己提的请求。"""
        from traceops.remediation.approval import ApprovalDecision
        from traceops.remediation.approval_store import ApprovalRequest, ApprovalStore

        store = ApprovalStore(tmp_path / "a.db", default_ttl_seconds=600)
        request = store.request(ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="carol",
            action="scale_pool", target="redis-pool", reason="self"))

        with pytest.raises(AccessDenied, match="separation"):
            store.decide(request.request_id, decision=ApprovalDecision.APPROVED,
                         approver="carol", reason="self-approved",
                         principal=Principal("carol", Role.APPROVER))
