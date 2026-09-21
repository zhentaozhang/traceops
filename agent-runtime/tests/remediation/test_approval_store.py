"""审批必须能**跨进程送达**，否则闭环不存在。

## 缺陷

`Approval` 是一个内存 frozen dataclass：无 ID、无持久化、无超时。
`RunStatus.WAITING_FOR_APPROVAL` 状态存在、`suspend_for_approval()` 已实现，
但**只在测试里被调用** —— 生产路径没有任何地方会因为"需要审批"而挂起并等待。

后果：PRD 5.5 把审批闭环标为 ✅，但它实际不成立：

- 审批请求无法到达任何审批人（没有可查询的请求记录）
- 审批决策无法回到调查进程（没有共享存储）
- 挂起的 run 永远不会被解除（没有超时策略）

这三条是"审批闭环"的定义，缺任一条都不成立。

## 本模块要立的三件事

1. **审批请求是一等实体**（有 ID、有计划哈希、有发起时间）
2. **请求可持久化并跨进程查询**（SQLite，与 RunLedger 同一套思路）
3. **超时是一等语义**（到期未决 → EXPIRED，而不是永远挂起）

## 为什么超时必须是显式状态而不是"自动拒绝"

自动拒绝会丢掉"有人可能还会批"这个事实，也会让审计看不到"请求过期了"。
EXPIRED 是第三种终态，与 APPROVED/REJECTED 并列。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from traceops.remediation.approval import ApprovalDecision
from traceops.remediation.approval_store import (
    ApprovalRequest,
    ApprovalRequestStatus,
    ApprovalStore,
)


class TestRequestIsARealEntity:
    def test_request_has_id_and_hash(self):
        request = ApprovalRequest.create(
            plan_id="plan-1", plan_hash="abc123", requested_by="agent-runtime",
            action="scale_pool", target="redis-connection-pool",
            reason="pool exhausted, propose to restore maxPoolSize",
        )
        assert request.request_id
        assert request.plan_hash == "abc123"
        assert request.status is ApprovalRequestStatus.PENDING

    def test_request_records_when_it_was_made(self):
        request = ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="w",
            action="a", target="t", reason="r")
        assert request.requested_at is not None


class TestStorePersistsAcrossProcesses:
    def test_request_survives_reopen(self, tmp_path):
        path = tmp_path / "approvals.db"
        store = ApprovalStore(path)
        request = ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="w", action="a", target="t", reason="r")
        store.request(request)

        reopened = ApprovalStore(path)
        found = reopened.get(request.request_id)
        assert found is not None, "an approval request must survive a process restart"
        assert found.plan_hash == "h"

    def test_pending_requests_are_listable(self, tmp_path):
        """审批人需要能"看到有哪些等待我批" —— 这是闭环的入口。"""
        store = ApprovalStore(tmp_path / "a.db")
        for i in range(3):
            store.request(ApprovalRequest.create(
                plan_id=f"p{i}", plan_hash=f"h{i}", requested_by="w",
                action="a", target="t", reason="r"))
        assert len(store.pending()) == 3


class TestDecisionClosesTheLoop:
    def test_decision_is_recorded_and_queryable(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.db")
        request = ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="w", action="a", target="t", reason="r")
        store.request(request)
        store.decide(request.request_id, decision=ApprovalDecision.APPROVED,
                     approver="sre-1", reason="looks right")

        found = store.get(request.request_id)
        assert found.status is ApprovalRequestStatus.APPROVED
        assert found.approver == "sre-1"

    def test_second_decision_is_rejected(self, tmp_path):
        """审批只能决定一次 —— 否则审计无法判断哪个是最终决定。"""
        store = ApprovalStore(tmp_path / "a.db")
        request = ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="w", action="a", target="t", reason="r")
        store.request(request)
        store.decide(request.request_id, decision=ApprovalDecision.APPROVED,
                     approver="sre-1", reason="ok")
        with pytest.raises(Exception):
            store.decide(request.request_id, decision=ApprovalDecision.REJECTED,
                         approver="sre-2", reason="changed my mind")

    def test_empty_approver_or_reason_rejected(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.db")
        request = ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="w", action="a", target="t", reason="r")
        store.request(request)
        with pytest.raises(ValueError):
            store.decide(request.request_id, decision=ApprovalDecision.APPROVED,
                         approver="  ", reason="ok")
        with pytest.raises(ValueError):
            store.decide(request.request_id, decision=ApprovalDecision.APPROVED,
                         approver="sre-1", reason="  ")


class TestTimeoutIsAFirstClassState:
    def test_expired_request_is_marked_expired_not_pending(self, tmp_path):
        """到期未决 → EXPIRED，而不是永远 PENDING。

        永远 PENDING 会让"仍活跃"的判断一直为真，
        挂起的 run 卡住整个事故生命周期。
        """
        store = ApprovalStore(tmp_path / "a.db", default_ttl_seconds=1)
        request = ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="w", action="a", target="t", reason="r")
        store.request(request)
        # 模拟时间流逝
        store.expire_overdue(now=datetime.now(UTC) + timedelta(seconds=5))

        found = store.get(request.request_id)
        assert found.status is ApprovalRequestStatus.EXPIRED

    def test_not_yet_due_stays_pending(self, tmp_path):
        store = ApprovalStore(tmp_path / "a.db", default_ttl_seconds=600)
        request = ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="w", action="a", target="t", reason="r")
        store.request(request)
        store.expire_overdue(now=datetime.now(UTC))
        assert store.get(request.request_id).status is ApprovalRequestStatus.PENDING

    def test_expired_cannot_be_decided(self, tmp_path):
        """过期后不能再批 —— 否则超时形同虚设。"""
        store = ApprovalStore(tmp_path / "a.db", default_ttl_seconds=1)
        request = ApprovalRequest.create(
            plan_id="p", plan_hash="h", requested_by="w", action="a", target="t", reason="r")
        store.request(request)
        store.expire_overdue(now=datetime.now(UTC) + timedelta(seconds=5))
        with pytest.raises(Exception):
            store.decide(request.request_id, decision=ApprovalDecision.APPROVED,
                         approver="sre-1", reason="late")
