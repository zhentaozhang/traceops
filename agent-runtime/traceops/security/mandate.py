"""授权凭证（Mandate）：审批通过后留下的**不可变**授权记录。

## 为什么审批状态不够

`ApprovalRequest.status == APPROVED` 只说"有人点了同意"。它答不了下面这些
问题，而这些正是复盘时要问的：

- **谁**批的？（`approver` 是个字符串，可以是任何东西，包括空）
- 依据**哪套规则**批的？（策略改了以后，旧审批还算不算数？）
- 授权**范围**是什么？（只对这份计划、这一个动作有效吗？）
- 这个授权**还能不能被撤销**？（计划改了、账号被停用之后呢？）

状态是**可变的**：字段被后面的人改一次，历史就没了。所以这里把授权本身
做成一条独立记录 —— 一旦授予就不再改写，只能**撤销**（撤销也是追加事实，
不是覆盖旧值）。

## 与 `Approval` / `plan_hash` 的关系

`plan_hash` 已经保证了"审批绑定的是这份计划的内容"。这条凭证在此之上补三件：

1. `policy_version` —— 授权依据的规则版本。策略收紧了，旧凭证照样有效，
   但你看得出它是在旧规则下发的。
2. `scope` —— 明确写死 plan_id / plan_hash / action / target 四项，
   `covers()` 逐项比对。只比 plan_hash 会漏掉"同一个计划被拿去对不同
   target 执行"。
3. `revoked_at` —— 第三种结局。`REJECTED` 是"没同意"，`EXPIRED` 是
   "没人看"，撤销是"同意了但后来不算了"。把撤销塞进 `REJECTED` 会让
   审计分不清"被拒绝"和"被追回"。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: 授权规则的版本。**改变授权判定逻辑**（不是改变审批界面）才动它。
#: 凭证里记下它，是为了回答"这条授权是按哪套规则发的"。
MANDATE_POLICY_REVISION = "approval-v1"

#: 授权强度。`explicit` = 有人明确点了同意；`inferred` = 由规则推导
#: （例如"只读演练无需审批"）。两者在审计里的分量完全不同，必须分开记。
STRENGTH_EXPLICIT = "explicit"
STRENGTH_INFERRED = "inferred"

#: 授权依据。`human_approval` 是 P0 唯一一条路径；
#: 其它依据（如"只读演练无需审批"）等真有那条路径时再加 ——
#: 先声明一个没人用的常量，等于给"授权依据"这个概念注水。
BASIS_HUMAN_APPROVAL = "human_approval"


class MandateScopeMismatch(Exception):
    """凭证的授权范围不覆盖这次调用。"""


@dataclass(frozen=True)
class Mandate:
    """一条不可变授权。字段一旦写入，只能靠 `revoked_at` 追加事实。"""

    strength: str
    policy_version: str
    basis: str
    actor: str | None
    scope: dict[str, Any] = field(default_factory=dict)
    granted_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.strength not in (STRENGTH_EXPLICIT, STRENGTH_INFERRED):
            raise ValueError(f"unknown mandate strength: {self.strength!r}")
        if not self.policy_version:
            raise ValueError("mandate requires a policy_version")
        for key in ("plan_id", "plan_hash", "action", "target"):
            if key not in self.scope:
                raise ValueError(f"mandate scope must pin {key!r}")

    @property
    def digest(self) -> str:
        """内容哈希：同一份授权重复入库能认出来，改一个字段就换一个值。"""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"),
                               default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def covers(self, *, plan_id: str, plan_hash: str, action: str, target: str) -> bool:
        """逐项比对授权范围。

        只比 `plan_hash` 是不够的：同一个计划可以对不同 target 执行，
        而"对哪个服务执行"恰恰是最该被授权钉住的那一项。
        """
        return (
            self.scope.get("plan_id") == plan_id
            and self.scope.get("plan_hash") == plan_hash
            and self.scope.get("action") == action
            and self.scope.get("target") == target
        )

    def require_covers(self, *, plan_id: str, plan_hash: str, action: str,
                       target: str) -> None:
        if self.covers(plan_id=plan_id, plan_hash=plan_hash, action=action, target=target):
            return
        raise MandateScopeMismatch(
            f"mandate scope {self.scope!r} does not cover "
            f"plan_id={plan_id!r} action={action!r} target={target!r} "
            f"(plan_hash={plan_hash!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "strength": self.strength,
            "policy_version": self.policy_version,
            "basis": self.basis,
            "actor": self.actor,
            "scope": dict(self.scope),
            "granted_at": self.granted_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Mandate":
        return cls(
            strength=data["strength"],
            policy_version=data["policy_version"],
            basis=data["basis"],
            actor=data.get("actor"),
            scope=dict(data.get("scope") or {}),
            granted_at=datetime.fromisoformat(data["granted_at"]),
        )
