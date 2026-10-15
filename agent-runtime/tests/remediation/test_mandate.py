"""授权凭证：审批通过之后，留下的是"能证明什么"而不是"某个人点过同意"。

`ApprovalRequest.status == APPROVED` 只回答"有人同意了"。复盘要问的是
更具体的四件事，而状态位一件都答不了：

1. **谁** —— `approver` 是自由字符串，可以是空、可以是别人的名字；
2. 按**哪套规则** —— 策略收紧了，旧审批还算数吗？
3. **范围** —— 只对这份计划的这个动作这个目标有效吗？
4. **能不能撤销** —— 计划改了、账号停了之后呢？

这些问题的共同点是：答案必须是**可核验的事实**，而不是"当时应该是这样"。
所以授权被做成独立记录，只追加、不覆盖。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traceops.remediation.approval import ApprovalDecision
from traceops.remediation.approval_store import (
    ApprovalRequest,
    ApprovalRequestStatus,
    ApprovalStore,
    IllegalApprovalTransition,
    MandateUnavailable,
)
from traceops.security.access import Principal, Role
from traceops.security.mandate import (
    BASIS_HUMAN_APPROVAL,
    MANDATE_POLICY_REVISION,
    STRENGTH_EXPLICIT,
    Mandate,
    MandateScopeMismatch,
)

SCOPE = {
    "plan_id": "plan-1",
    "plan_hash": "hash-abc",
    "action": "restart_pod",
    "target": "redis",
}


def _request(**overrides) -> ApprovalRequest:
    kwargs = dict(
        plan_id=SCOPE["plan_id"],
        plan_hash=SCOPE["plan_hash"],
        requested_by="alice",
        action=SCOPE["action"],
        target=SCOPE["target"],
        reason="error rate 42% for 10 minutes",
        ttl_seconds=600,
    )
    kwargs.update(overrides)
    return ApprovalRequest.create(**kwargs)


def _store() -> ApprovalStore:
    store = ApprovalStore()
    store.request(_request())
    return store


def _approver(name: str = "bob") -> Principal:
    return Principal(name=name, role=Role.APPROVER)


class TestTheMandatePinsItsOwnScope:
    def test_a_mandate_needs_every_scope_field(self):
        with pytest.raises(ValueError, match="action"):
            Mandate(
                strength=STRENGTH_EXPLICIT,
                policy_version=MANDATE_POLICY_REVISION,
                basis=BASIS_HUMAN_APPROVAL,
                actor="bob",
                scope={"plan_id": "p", "plan_hash": "h", "target": "t"},
            )

    def test_covers_requires_every_field_to_match(self):
        mandate = Mandate(
            strength=STRENGTH_EXPLICIT, policy_version=MANDATE_POLICY_REVISION,
            basis=BASIS_HUMAN_APPROVAL, actor="bob", scope=dict(SCOPE))
        assert mandate.covers(**SCOPE)
        # 只比 plan_hash 会漏掉"同一个计划被拿去打不同目标"
        drifted = {**SCOPE, "target": "payment"}
        assert not mandate.covers(**drifted)
        with pytest.raises(MandateScopeMismatch, match="payment"):
            mandate.require_covers(**drifted)

    def test_the_digest_moves_with_the_content(self):
        granted = datetime(2026, 10, 30, 10, 5, tzinfo=UTC)
        base = Mandate(strength=STRENGTH_EXPLICIT, policy_version=MANDATE_POLICY_REVISION,
                       basis=BASIS_HUMAN_APPROVAL, actor="bob", scope=dict(SCOPE),
                       granted_at=granted)
        same = Mandate(strength=STRENGTH_EXPLICIT, policy_version=MANDATE_POLICY_REVISION,
                       basis=BASIS_HUMAN_APPROVAL, actor="bob", scope=dict(SCOPE),
                       granted_at=granted)
        other = Mandate(strength=STRENGTH_EXPLICIT, policy_version=MANDATE_POLICY_REVISION,
                        basis=BASIS_HUMAN_APPROVAL, actor="carol", scope=dict(SCOPE),
                        granted_at=granted)
        assert base.digest == same.digest
        assert base.digest != other.digest

    def test_an_unknown_strength_is_rejected(self):
        with pytest.raises(ValueError, match="strength"):
            Mandate(strength="probably", policy_version="v1",
                    basis=BASIS_HUMAN_APPROVAL, actor="bob", scope=dict(SCOPE))


class TestApprovingGrantsAMandate:
    def test_approval_records_who_and_under_which_rules(self):
        store = _store()
        request_id = store.pending()[0].request_id
        decided = store.decide(request_id, decision=ApprovalDecision.APPROVED,
                               approver="bob", reason="looks right")
        assert decided.status is ApprovalRequestStatus.APPROVED
        assert decided.mandate is not None
        assert decided.mandate["policy_version"] == MANDATE_POLICY_REVISION
        assert decided.mandate["strength"] == STRENGTH_EXPLICIT
        assert decided.mandate["scope"] == SCOPE
        # 没传 principal 时 actor 是 None —— 见下面那个测试
        assert decided.mandate["actor"] is None

    def test_an_unauthenticated_approval_does_not_pretend_to_have_an_actor(self):
        """没有身份上下文时记 None，而不是把 approver 字符串当成已认证主体。"""
        store = _store()
        request_id = store.pending()[0].request_id
        decided = store.decide(request_id, decision=ApprovalDecision.APPROVED,
                               approver="bob", reason="looks right")
        assert decided.mandate["actor"] is None

    def test_with_a_principal_the_actor_is_the_authenticated_name(self):
        store = _store()
        request_id = store.pending()[0].request_id
        decided = store.decide(request_id, decision=ApprovalDecision.APPROVED,
                               approver="bob", reason="looks right",
                               principal=_approver("bob"))
        assert decided.mandate["actor"] == "bob"

    def test_rejection_grants_nothing(self):
        store = _store()
        request_id = store.pending()[0].request_id
        decided = store.decide(request_id, decision=ApprovalDecision.REJECTED,
                               approver="bob", reason="wrong target")
        assert decided.mandate is None
        assert not decided.has_live_mandate


class TestRevocationIsAThirdOutcome:
    def test_a_revoked_mandate_stops_authorising(self):
        store = _store()
        request_id = store.pending()[0].request_id
        store.decide(request_id, decision=ApprovalDecision.APPROVED,
                     approver="bob", reason="ok")
        revoked = store.revoke(request_id, reason="incident self-resolved")
        assert revoked.status is ApprovalRequestStatus.APPROVED
        assert revoked.revoked_at is not None
        assert not revoked.has_live_mandate
        with pytest.raises(MandateUnavailable, match="revoked"):
            store.require_mandate(request_id, **SCOPE)

    def test_revocation_records_who_did_it(self):
        store = _store()
        request_id = store.pending()[0].request_id
        store.decide(request_id, decision=ApprovalDecision.APPROVED,
                     approver="bob", reason="ok")
        revoked = store.revoke(request_id, reason="plan changed", actor="carol")
        assert "carol" in revoked.revoked_reason

    def test_a_pending_request_cannot_be_revoked(self):
        store = _store()
        request_id = store.pending()[0].request_id
        with pytest.raises(IllegalApprovalTransition, match="APPROVED"):
            store.revoke(request_id, reason="nothing to revoke")

    def test_revoking_twice_is_refused(self):
        store = _store()
        request_id = store.pending()[0].request_id
        store.decide(request_id, decision=ApprovalDecision.APPROVED,
                     approver="bob", reason="ok")
        store.revoke(request_id, reason="first")
        with pytest.raises(IllegalApprovalTransition, match="already revoked"):
            store.revoke(request_id, reason="second")

    def test_a_reason_is_required(self):
        store = _store()
        request_id = store.pending()[0].request_id
        store.decide(request_id, decision=ApprovalDecision.APPROVED,
                     approver="bob", reason="ok")
        with pytest.raises(ValueError, match="reason"):
            store.revoke(request_id, reason="   ")


class TestExecutionAsksForTheMandateNotTheStatus:
    def test_a_scope_drift_is_caught_even_though_the_status_is_approved(self):
        store = _store()
        request_id = store.pending()[0].request_id
        store.decide(request_id, decision=ApprovalDecision.APPROVED,
                     approver="bob", reason="ok")
        with pytest.raises(MandateScopeMismatch):
            store.require_mandate(request_id, **{**SCOPE, "target": "payment"})

    def test_a_plan_hash_change_is_caught(self):
        store = _store()
        request_id = store.pending()[0].request_id
        store.decide(request_id, decision=ApprovalDecision.APPROVED,
                     approver="bob", reason="ok")
        with pytest.raises(MandateScopeMismatch):
            store.require_mandate(request_id, **{**SCOPE, "plan_hash": "hash-edited"})

    def test_a_matching_call_succeeds(self):
        store = _store()
        request_id = store.pending()[0].request_id
        store.decide(request_id, decision=ApprovalDecision.APPROVED,
                     approver="bob", reason="ok", principal=_approver("bob"))
        mandate = store.require_mandate(request_id, **SCOPE)
        assert mandate.actor == "bob"

    def test_a_pending_request_has_no_mandate_yet(self):
        store = _store()
        request_id = store.pending()[0].request_id
        with pytest.raises(MandateUnavailable, match="no mandate"):
            store.require_mandate(request_id, **SCOPE)


class TestTheMandateSurvivesARestart:
    def test_it_is_read_back_from_disk(self, tmp_path):
        """审批跨进程：凭证必须真的落在库里，而不是活在对象上。"""
        path = tmp_path / "approvals.db"
        store = ApprovalStore(path)
        store.request(_request())
        request_id = store.pending()[0].request_id
        store.decide(request_id, decision=ApprovalDecision.APPROVED,
                     approver="bob", reason="ok", principal=_approver("bob"))

        reopened = ApprovalStore(path)
        mandate = reopened.require_mandate(request_id, **SCOPE)
        assert mandate.actor == "bob"
        assert mandate.policy_version == MANDATE_POLICY_REVISION

    def test_an_old_database_gets_the_new_columns(self, tmp_path):
        """老库是 CREATE TABLE IF NOT EXISTS 建的，补列只能靠 ALTER。

        不做迁移的话，升级后第一次读就会 "no such column: mandate" ——
        而且是**线上第一次审批**才炸。
        """
        import sqlite3

        path = tmp_path / "legacy.db"
        legacy = sqlite3.connect(str(path))
        legacy.executescript(
            "CREATE TABLE approval_request ("
            "request_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, plan_hash TEXT NOT NULL,"
            "requested_by TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL,"
            "reason TEXT NOT NULL, status TEXT NOT NULL, requested_at TEXT NOT NULL,"
            "expires_at TEXT NOT NULL, approver TEXT, decided_at TEXT,"
            "decision_reason TEXT);")
        legacy.commit()
        legacy.close()

        store = ApprovalStore(path)
        store.request(_request())
        request_id = store.pending()[0].request_id
        decided = store.decide(request_id, decision=ApprovalDecision.APPROVED,
                               approver="bob", reason="ok")
        assert decided.mandate is not None
