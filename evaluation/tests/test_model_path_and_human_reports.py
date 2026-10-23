"""评测自己要说清两件事：跑的是哪条路，以及报告去哪了。

- 没有模型端点时，`mode` 必须是 `rules-only`；把这些数字当成 agent 能力是错的。
- 每个场景的诊断必须能被**人**读到（此前报告对象在 harness 里被丢掉）。
"""
from __future__ import annotations

import json

import pytest

from evaluation import model_check, runner
from evaluation.harness import run_scenario
from evaluation.scenario import load_scenarios
from traceops.runtime.model import ModelResponse


def _scenario(scenario_id: str = "F-000"):
    for candidate in load_scenarios(runner.DEFAULT_SCENARIOS_DIR):
        if candidate.scenario_id == scenario_id:
            return candidate
    raise AssertionError(f"scenario {scenario_id} not found")


class TestTheReportSurvivesScoring:
    def test_the_outcome_carries_the_report(self):
        outcome = run_scenario(_scenario())
        assert outcome.report is not None
        assert outcome.report.diagnosis_status is not None

    def test_the_report_is_not_serialised(self):
        outcome = run_scenario(_scenario())
        assert "report" not in outcome.to_dict()


class TestTheRunnerSaysWhichPathItRan:
    def test_no_endpoint_means_rules_only(self, monkeypatch):
        monkeypatch.delenv("TRACEOPS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("TRACEOPS_LLM_MODEL", raising=False)
        args = type("A", (), {"model_base_url": None, "model_name": None,
                              "model_api_key": None})()
        assert runner._build_model(args) is None

    def test_a_full_configuration_builds_a_client(self):
        args = type("A", (), {"model_base_url": "http://127.0.0.1:9", "model_name": "m",
                              "model_api_key": None})()
        model = runner._build_model(args)
        assert model is not None and model.name

    def test_the_payload_records_the_mode(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TRACEOPS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("TRACEOPS_LLM_MODEL", raising=False)
        out = tmp_path / "latest.json"
        code = runner.main(["--scenario", "F-000", "--out", str(out),
                            "--reports-dir", str(tmp_path / "reports")])
        assert code == 0
        payload = json.loads(out.read_text())
        assert payload["mode"] == "rules-only"

    def test_human_reports_are_written_per_scenario(self, tmp_path):
        outcome = run_scenario(_scenario())
        written = runner._write_human_reports([outcome], tmp_path / "reports")
        assert written == 1
        body = (tmp_path / "reports" / "F-000.md").read_text()
        assert body.startswith("# F-000")
        assert "结论" in body and "凭什么" in body


class TestTheModelCheckDoesNotPretend:
    def test_unconfigured_is_unverified_not_success(self, monkeypatch, capsys):
        monkeypatch.delenv("TRACEOPS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("TRACEOPS_LLM_MODEL", raising=False)
        assert model_check.main([]) == model_check.UNVERIFIED
        assert "UNVERIFIED" in capsys.readouterr().err

    def test_a_working_endpoint_reports_ok(self, monkeypatch, capsys):
        class _Stub:
            name = "stub"

            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def propose(self, request):
                return ModelResponse(hypotheses=[], input_tokens=11, output_tokens=7)

        monkeypatch.setattr("traceops.runtime.openai_client.OpenAICompatibleModel", _Stub)
        code = model_check.main(["--base-url", "http://stub", "--model", "stub-model"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["status"] == "OK"
        assert payload["input_tokens"] == 11

    def test_a_broken_endpoint_fails_loudly(self, monkeypatch, capsys):
        class _Broken:
            name = "broken"

            def __init__(self, **kwargs):
                pass

            def propose(self, request):
                raise RuntimeError("connection refused")

        monkeypatch.setattr("traceops.runtime.openai_client.OpenAICompatibleModel", _Broken)
        code = model_check.main(["--base-url", "http://stub", "--model", "m"])
        assert code == 1
        assert "FAILED" in capsys.readouterr().err
