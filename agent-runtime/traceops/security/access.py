"""授权判定：角色、动作、以及**审批权与执行权的分离**。

## 为什么这个模块必须是窄的

做一套抽象 RBAC、然后声称"权限已实现"，是最容易的自欺 —— 它不改变任何真实行为。
所以这里只覆盖本项目**真实需要权限**的四类动作：

| 动作 | 风险 | 允许的角色 |
|---|---|---|
| 读事故 / 结论 / 证据 | 低 | 全部 |
| 人工创建事故 | 低-中 | OPERATOR 及以上 |
| **审批修复计划** | **高**（批准后真的改环境） | APPROVER |
| 执行已批准的计划 | 高 | EXECUTOR |

核心不是"用户管理"，而是**审批权 ≠ 执行权**：批准者不能自己执行，
执行者不能自己批准。否则"复核"只是同一个人点两次。

## 三条硬约束

1. **分离职责**：`require_separation_of_duty` 拒绝自批自放
2. **拒绝而不是降级**：权限不足抛 `AccessDenied`，绝不"以只读模式继续" ——
   静默降级会让调用方以为动作成功了
3. **fail-closed**：未知动作一律拒绝。允许"没规则就放行"等于留了一个后门，
   任何拼错的动作名都能绕过全部检查

## 边界（诚实声明）

- **不做认证**：密码 / 会话 / JWT 属于部署层（OIDC / SSO）。
  本模块只做**授权判定**，身份由调用方传入
- **不持久化用户表**：没有用户库，也不打算在这层建一个
- **不替换现有审批流**：它给 `ApprovalStore.decide` 增加前置判定，
  不改变既有语义
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Action(str, Enum):
    """可被授权的动作。集合刻意很小 —— 只包含真实存在的入口。"""

    READ_INCIDENT = "read_incident"
    CREATE_INCIDENT = "create_incident"
    APPROVE_REMEDIATION = "approve_remediation"
    EXECUTE_REMEDIATION = "execute_remediation"


class Role(str, Enum):
    """角色。

    权限用**显式集合**而不是继承链：继承会让"executor 是否能读证据"
    这类问题需要向上追溯，而这里的规则应当一眼可读。
    """

    VIEWER = "VIEWER"
    OPERATOR = "OPERATOR"
    APPROVER = "APPROVER"
    EXECUTOR = "EXECUTOR"

    @property
    def permissions(self) -> frozenset[Action]:
        return _ROLE_PERMISSIONS[self]


#: 每个角色允许的动作。
#:
#: 刻意让 APPROVER 与 EXECUTOR **互斥**：审批权不含执行权、执行权不含审批权。
#: 这是约束 1（分离职责）在数据结构上的表达 ——
#: 靠函数里写 if 判断容易在某条路径上漏掉，靠权限集合则不可能漏。
_ROLE_PERMISSIONS: dict[Role, frozenset[Action]] = {
    Role.VIEWER: frozenset({Action.READ_INCIDENT}),
    Role.OPERATOR: frozenset({Action.READ_INCIDENT, Action.CREATE_INCIDENT}),
    Role.APPROVER: frozenset({Action.READ_INCIDENT, Action.APPROVE_REMEDIATION}),
    Role.EXECUTOR: frozenset({Action.READ_INCIDENT, Action.EXECUTE_REMEDIATION}),
}


class AccessDenied(Exception):
    """权限不足。**必须显式拒绝，不能静默降级。**"""


@dataclass(frozen=True)
class Principal:
    """调用者身份。

    只有两项：谁（name）、什么角色（role）。
    没有 email / 部门 / 组织树 —— 那些不参与授权判定，加了只会膨胀。
    """

    name: str
    role: Role

    def identity(self) -> str:
        """审计用的稳定标识。

        为什么拼上角色：同一个人换了角色之后，审计里两次记录必须可区分 ——
        否则"当时他以什么权限做的"就查不出来了。
        """
        return f"{self.name}:{self.role.value}"


def authorize(principal: Principal, action: "Action | str") -> None:
    """判定并**抛异常**。

    为什么不用 `bool` 返回：布尔返回最容易被误用 ——
    调用方写 `if not allowed: pass` 就静默跳过了检查。
    抛异常迫使每个调用点显式处理。
    """
    if not isinstance(principal, Principal):
        raise AccessDenied(f"invalid principal: {principal!r}")

    resolved = _resolve_action(action)
    if resolved not in principal.role.permissions:
        raise AccessDenied(
            f"role {principal.role.value.lower()} may not perform "
            f"{_action_name(action)}")


def _resolve_action(action: "Action | str") -> Action:
    """解析动作；未知动作一律失败（fail-closed）。

    允许"没规则就放行"等于留后门：任何拼错的动作名都能绕过全部检查。
    """
    if isinstance(action, Action):
        return action
    try:
        return Action(action)
    except ValueError as exc:
        raise AccessDenied(f"unknown action: {action!r}") from exc


def _action_name(action: "Action | str") -> str:
    return action.value if isinstance(action, Action) else str(action)


def require_separation_of_duty(*, requested_by: str, approver: str) -> None:
    """申请人与审批人必须是不同的人。

    这是"审批"之所以有意义的全部理由：同一个人自己批自己，
    复核环节等于不存在，而审计上却显示"已审批"——那比没有审批更危险。
    """
    if not requested_by or not approver:
        raise AccessDenied("separation of duty requires both requester and approver")
    if requested_by.strip().lower() == approver.strip().lower():
        raise AccessDenied(
            f"separation of duty violated: {requested_by!r} may not approve a "
            f"remediation they requested themselves")


def can_approve(principal: Principal, *, requested_by: str) -> bool:
    """便捷判定：该主体能否审批这条请求（含职责分离）。

    与 `authorize` 的区别：这里要同时看权限**和**请求归属，
    所以返回布尔供调用方做 UI 提示；真正的执行路径仍应 `authorize`。
    """
    if Action.APPROVE_REMEDIATION not in principal.role.permissions:
        return False
    return principal.name.strip().lower() != (requested_by or "").strip().lower()
