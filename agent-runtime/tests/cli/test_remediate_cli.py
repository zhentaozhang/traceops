"""受控修复的操作员入口：三步之间靠审批库连接，每一步都真的过闸。

这些测试走的是**真实 CLI 路径**（`main(argv)`），不是直接调库函数 ——
之前那批 remediation 测试的问题正是"库函数都对、但没有入口"。
"""
from __future__ import annotations

import json

import pytest

from traceops.cli.remediate import main
from traceops.remediation.backends import BackendUnavailable, executor_from_config

PLAN_ARGS = [
    "--incident", "inc-1",
    "--action", "scale_replicas",
    "--target", "order-service",
    "--parameters", '{"replicas": 4}',
    "--rollback", '{"action": "scale_replicas", "parameters": {"replicas": 2}}',
    "--reason", "shed load",
    "--error-rate-below", "0.05",
]


def _run(capsys, db, *argv: str) -> tuple[int, dict]:
    code = main(["--approval-db", str(db), *argv])
    return code, json.loads(capsys.readouterr().out)


def _plan(capsys, db, *, who: str = "alice") -> str:
    code, payload = _run(capsys, db, "plan", *PLAN_ARGS, "--as", who)
    assert code == 0, payload
    assert payload["outcome"] == "awaiting-approval"
    return payload["request_id"]


def _approve(capsys, db, request_id: str, *, who: str = "bob") -> None:
    code, payload = _run(capsys, db, "approve", "--request", request_id,
                         "--as", who, "--reason", "approved for the window")
    assert code == 0, payload
    assert payload["status"] == "APPROVED"


class TestTheGateIsOpenable:
    def test_a_plan_becomes_an_approval_request_bound_to_its_content(self, capsys, tmp_path):
        request_id = _plan(capsys, tmp_path / "approvals.db")

        assert request_id.startswith("apr-")

    def test_an_approved_request_can_be_executed_as_a_dry_run(self, capsys, tmp_path):
        db = tmp_path / "approvals.db"
        request_id = _plan(capsys, db)
        _approve(capsys, db, request_id)

        code, payload = _run(capsys, db, "execute", *PLAN_ARGS,
                             "--request", request_id, "--as", "carol", "--dry-run")

        assert code == 0
        assert payload["outcome"] == "dry_run"
        # 干跑不是"成功"：它没有改动任何东西
        assert payload["settled"] is False


class TestTheGateStaysShut:
    def test_a_pending_request_cannot_be_executed(self, capsys, tmp_path):
        db = tmp_path / "approvals.db"
        request_id = _plan(capsys, db)

        code, payload = _run(capsys, db, "execute", *PLAN_ARGS,
                             "--request", request_id, "--dry-run")

        assert code == 2
        assert "carries no mandate" in payload["reasons"][0]

    def test_changing_a_parameter_after_approval_invalidates_it(self, capsys, tmp_path):
        db = tmp_path / "approvals.db"
        request_id = _plan(capsys, db)
        _approve(capsys, db, request_id)

        tampered = list(PLAN_ARGS)
        tampered[tampered.index('{"replicas": 4}')] = '{"replicas": 40}'
        code, payload = _run(capsys, db, "execute", *tampered,
                             "--request", request_id, "--dry-run")

        assert code == 2
        assert "does not match the approved plan" in payload["reasons"][0]

    def test_only_an_approver_role_may_approve(self, capsys, tmp_path):
        db = tmp_path / "approvals.db"
        request_id = _plan(capsys, db)

        code, payload = _run(capsys, db, "approve", "--request", request_id,
                             "--as", "viewer-v", "--role", "VIEWER", "--reason", "ok")

        assert code == 3
        assert payload["outcome"] == "denied"

    def test_the_person_who_asked_cannot_be_the_one_who_approves(self, capsys, tmp_path):
        db = tmp_path / "approvals.db"
        request_id = _plan(capsys, db, who="alice")

        code, payload = _run(capsys, db, "approve", "--request", request_id,
                             "--as", "alice", "--reason", "self-approval")

        assert code != 0

    def test_a_write_action_without_an_executor_config_is_refused(self, capsys, tmp_path):
        db = tmp_path / "approvals.db"
        request_id = _plan(capsys, db)
        _approve(capsys, db, request_id)

        code, payload = _run(capsys, db, "execute", *PLAN_ARGS,
                             "--request", request_id, "--as", "carol")

        assert code == 2
        assert "refusing to execute a write action" in payload["reasons"][0]


class TestBackends:
    def test_no_configuration_means_no_write_path(self):
        with pytest.raises(BackendUnavailable, match="refusing to execute"):
            executor_from_config(None)

    def test_a_command_template_only_substitutes_the_target(self, tmp_path):
        config = tmp_path / "exec.json"
        config.write_text(json.dumps({"scale_replicas": ["kubectl", "scale",
                                                        "deploy/$TARGET", "--replicas"]}))
        executor = executor_from_config(config)

        assert executor.commands["scale_replicas"][2] == "deploy/$TARGET"
