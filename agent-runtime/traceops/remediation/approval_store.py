"""审批请求的持久化存储：让审批闭环真的能跨进程成立。

## 为什么需要（PRD 5.5 标 ✅ 但实际不成立的第三处）

`remediation/approval.py` 的 `Approval` 是一个内存 frozen dataclass：
无 ID、无持久化、无超时。`RunStatus.WAITING_FOR_APPROVAL` 状态存在、
`suspend_for_approval()` 已实现，但**只在测试里被调用**。

"审批闭环"由三件事定义，缺任一条都不成立：

1. **审批请求能到达审批人** —— 需要一个可查询的请求记录
2. **审批决策能回到调查进程** —— 需要共享存储，不是内存对象
3. **挂起的 run 不会永远挂起** —— 需要超时语义

本模块提供前两件，并把第三件做成**显式状态**（EXPIRED）而不是"自动拒绝"。

## 为什么 EXPIRED 是第三种终态，而不是"超时即拒绝"

自动拒绝会丢掉"有人可能还会批"这个事实；更重要的是审计看不到
"这个请求过期了" —— 那会让后续复盘误以为"有人拒绝了这个修复方案"。
两种情况的处置完全不同：一个要重新发起，一个要追究为什么没人看。
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from traceops.remediation.approval import ApprovalDecision
from traceops.security.access import Principal


class ApprovalRequestStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self is not ApprovalRequestStatus.PENDING


SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_request (
    request_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    approver TEXT,
    decided_at TEXT,
    decision_reason TEXT
);
"""


def _with_ttl(request: ApprovalRequest, ttl_seconds: float) -> ApprovalRequest:
    """按 store 的 TTL 重新计算 expires_at（保留其它字段）。"""
    from dataclasses import replace

    return replace(request,
                   expires_at=request.requested_at + timedelta(seconds=ttl_seconds))


@dataclass(frozen=True)
class ApprovalRequest:
    """一条审批请求。

    `plan_hash` 是必须的：审批绑定的是**具体执行计划**，参数一变原审批作废
    （与 `execution.verify_approval_matches` 同源）。
    """

    request_id: str
    plan_id: str
    plan_hash: str
    requested_by: str
    action: str
    target: str
    reason: str
    status: ApprovalRequestStatus
    requested_at: datetime
    expires_at: datetime
    approver: str | None = None
    decided_at: datetime | None = None
    decision_reason: str | None = None

    @classmethod
    def create(cls, *, plan_id: str, plan_hash: str, requested_by: str,
               action: str, target: str, reason: str,
               ttl_seconds: float = 3600) -> "ApprovalRequest":
        now = datetime.now(UTC)
        return cls(
            request_id=f"apr-{uuid.uuid4()}",
            plan_id=plan_id, plan_hash=plan_hash, requested_by=requested_by,
            action=action, target=target, reason=reason,
            status=ApprovalRequestStatus.PENDING,
            requested_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )


class ApprovalStore:
    """审批请求的 SQLite 存储。

    用 SQLite 而不是内存，理由与 `RunLedger` 相同：审批天然跨进程 ——
    调查进程发起、审批人的界面读取、决策再回到调查进程。
    内存对象无法跨进程传递，所以"闭环"在内存实现下永远不成立。
    """

    def __init__(self, path: str | Path | None = None, *,
                 default_ttl_seconds: float = 3600) -> None:
        self._path = Path(path) if path is not None else None
        self.default_ttl_seconds = default_ttl_seconds
        self._lock = threading.Lock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        else:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- 写入 ----
    def request(self, request: ApprovalRequest) -> ApprovalRequest:
        """登记一条审批请求。

        TTL 由 store 统一决定（因为超时策略属于部署配置，不属于单条请求）。
        若调用方给了更短/更长的 expires_at，以 store 的 default 为准 ——
        否则"超时策略"会散落在每个调用点。
        """
        request = _with_ttl(request, self.default_ttl_seconds)
        with self._lock:
            self._conn.execute(
                "INSERT INTO approval_request (request_id, plan_id, plan_hash, requested_by, "
                "action, target, reason, status, requested_at, expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (request.request_id, request.plan_id, request.plan_hash,
                 request.requested_by, request.action, request.target, request.reason,
                 request.status.value, request.requested_at.isoformat(),
                 request.expires_at.isoformat()))
            self._conn.commit()
        return request

    def decide(self, request_id: str, *, decision: ApprovalDecision, approver: str,
               reason: str, principal: "Principal | None" = None) -> ApprovalRequest:
        """记录决策。只能决定一次 —— 否则审计无法判断哪个是最终决定。

        `principal` 提供时**强制授权判定**（权限 + 职责分离）。
        为什么让它可选：既有的调用点（管理脚本、测试）没有身份上下文，
        强塞会让它们全部失败；但**所有面向用户的路径都必须传**。
        """
        if not approver or not approver.strip():
            raise ValueError("approver is required")
        if not reason or not reason.strip():
            raise ValueError("decision reason is required")

        if principal is not None:
            from traceops.security.access import Action, authorize, require_separation_of_duty

            # 权限：只有 APPROVER 能批
            authorize(principal, Action.APPROVE_REMEDIATION)
            current_for_duty = self.get(request_id)
            if current_for_duty is None:
                raise KeyError(f"unknown approval request: {request_id}")
            # 职责分离：不能批自己提的
            require_separation_of_duty(
                requested_by=current_for_duty.requested_by,
                approver=principal.name)
            # 声明的 approver 必须与实际主体一致，否则审计记录的是假人
            if approver.strip().lower() != principal.name.strip().lower():
                raise ValueError(
                    f"approver {approver!r} does not match authenticated "
                    f"principal {principal.name!r}")

        with self._lock:
            current = self._get_locked(request_id)
            if current is None:
                raise KeyError(f"unknown approval request: {request_id}")
            if current.status.is_terminal:
                raise IllegalApprovalTransition(
                    f"request {request_id} is already {current.status.value}")
            status = (ApprovalRequestStatus.APPROVED
                      if decision is ApprovalDecision.APPROVED
                      else ApprovalRequestStatus.REJECTED)
            self._conn.execute(
                "UPDATE approval_request SET status=?, approver=?, decided_at=?, decision_reason=? "
                "WHERE request_id=?", (status.value, approver.strip(), datetime.now(UTC).isoformat(),
                                       reason.strip(), request_id))
            self._conn.commit()
            return self._get_locked(request_id)

    def expire_overdue(self, *, now: datetime | None = None) -> int:
        """把过期未决的请求标为 EXPIRED，返回受影响条数。

        这是"挂起的 run 不会永远挂起"的落地方式：到期后状态显式变化，
        上层据此把 run 从 WAITING_FOR_APPROVAL 推进到终态。
        """
        moment = now or datetime.now(UTC)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE approval_request SET status=? "
                "WHERE status=? AND expires_at <= ?",
                (ApprovalRequestStatus.EXPIRED.value, ApprovalRequestStatus.PENDING.value,
                 moment.isoformat()))
            self._conn.commit()
            return cursor.rowcount

    # ---- 查询 ----
    def get(self, request_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._get_locked(request_id)

    def pending(self) -> list[ApprovalRequest]:
        """等待审批的请求 —— 审批人界面的入口。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approval_request WHERE status=? ORDER BY requested_at",
                (ApprovalRequestStatus.PENDING.value,)).fetchall()
            return [self._row_to_request(row) for row in rows]

    def all_requests(self) -> list[ApprovalRequest]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approval_request ORDER BY requested_at").fetchall()
            return [self._row_to_request(row) for row in rows]

    # ---- 内部 ----
    def _get_locked(self, request_id: str) -> ApprovalRequest | None:
        row = self._conn.execute(
            "SELECT * FROM approval_request WHERE request_id=?", (request_id,)).fetchone()
        return self._row_to_request(row) if row else None

    @staticmethod
    def _row_to_request(row: tuple) -> ApprovalRequest:
        (request_id, plan_id, plan_hash, requested_by, action, target, reason,
         status, requested_at, expires_at, approver, decided_at, decision_reason) = row
        return ApprovalRequest(
            request_id=request_id, plan_id=plan_id, plan_hash=plan_hash,
            requested_by=requested_by, action=action, target=target, reason=reason,
            status=ApprovalRequestStatus(status),
            requested_at=datetime.fromisoformat(requested_at),
            expires_at=datetime.fromisoformat(expires_at),
            approver=approver,
            decided_at=datetime.fromisoformat(decided_at) if decided_at else None,
            decision_reason=decision_reason,
        )


class IllegalApprovalTransition(Exception):
    """对已终结的审批请求再次决策。"""
