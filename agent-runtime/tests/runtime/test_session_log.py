"""会话记录：一次调查"怎么走到结论的"必须能被回放。

报告回答"结论是什么"，会话回答"为什么是这个结论"。没有它，三个运维最常问的
问题只能去翻进程日志 —— 而进程日志重启即丢、也没有 budget 余额。

这个文件盯住四件事：

1. 不配就没有：`session=None` 时一个文件都不该出现（默认行为不变）；
2. 顺序与事实：工具调用、参数摘要、返回大小、预算消耗、结束原因都在；
3. 有上限：写满 `MAX_EVENTS` 之后只留一条 `session.truncated`；
4. 报告能指回去：`report.session_ref` 非空且文件真的存在。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetManager
from traceops.runtime.session import MAX_EVENTS, SessionRecorder, read_session, recorder_for, safe_name


class _Adapters:
    """最小数据源：两条证据足够跑完一次调查。"""

    @staticmethod
    def _at(start: str) -> int:
        return int(datetime.fromisoformat(start).timestamp()) + 60

    def query_service_error_rate(self, *, service, start, end):
        moment = self._at(start)
        return {"status": "success", "data": {"resultType": "matrix", "result": [{
            "metric": {"service": service},
            "values": [[moment - 60, "0.01"], [moment, "0.42"]],
        }]}}

    def query_service_logs(self, *, service, start, end, filter=None):
        moment = self._at(start)
        return {"status": "success", "data": {"resultType": "streams", "result": [{
            "stream": {"service": service, "level": "error"},
            "values": [[str(moment * 1_000_000_000), "ERROR redis timeout - pool exhausted"]],
        }]}}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        moment = self._at(start)
        return {"traces_considered": 1, "batches": [{
            "resource": {"service.name": service},
            "spans": [{"traceID": "a" * 32, "spanID": "1" * 16,
                       "operationName": "redis.command",
                       "startTime": moment * 1_000_000, "duration": 2_000_000}]}]}


def _request(run_id: str = "run-session-1") -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id=run_id,
        incident_id="inc-1",
        service_hint="order-service",
        fault_start=datetime(2026, 10, 30, 10, 5, tzinfo=UTC),
        window_start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 30, 10, 6, tzinfo=UTC),
        alert_summary="checkout p99 3.1s",
    )


def _run(*, session) -> tuple[object, RunContext]:
    from traceops.evidence.store import EvidenceStore
    from traceops.tools.gateway import ToolGateway, register_builtin_tools

    budget = BudgetManager(max_tool_calls=12, deadline_seconds=60)
    gateway = ToolGateway()
    register_builtin_tools(gateway, _Adapters())
    ctx = RunContext(store=EvidenceStore(), adapters=None, budget=budget,
                     gateway=gateway, session=session)
    return run_investigation(_request(), ctx), ctx


class TestNoConfigurationNoFiles:
    def test_without_a_session_nothing_is_written_and_the_report_points_nowhere(self) -> None:
        result, _ctx = _run(session=None)
        assert result.report is not None
        assert result.report.session_ref is None


class TestTheSessionRecordsHowItWent:
    def test_the_file_holds_start_tool_calls_and_the_ending(self, tmp_path) -> None:
        result, _ctx = _run(session=str(tmp_path))
        assert result.report is not None
        path = tmp_path / "run-session-1.jsonl"
        assert path.exists(), list(tmp_path.iterdir())
        assert result.report.session_ref == str(path)

        events = read_session(path)
        kinds = [event["kind"] for event in events]
        assert kinds[0] == "investigation.start"
        assert kinds[-1] == "investigation.end"
        assert "tool_call" in kinds

        # 序号连续：缺号意味着有人绕过 record() 直接写文件
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))

        tool_calls = [event for event in events if event["kind"] == "tool_call"]
        assert {event["tool"] for event in tool_calls} >= {"query_service_error_rate", "query_service_logs"}
        assert all(event["ok"] for event in tool_calls)
        assert all(event["payload_bytes"] and event["payload_bytes"] > 0 for event in tool_calls)

        ending = events[-1]
        assert ending["status"] in {"SUPPORTED", "INCONCLUSIVE", "REFUTED"}
        assert ending["tool_calls"] == len(tool_calls)
        assert ending["events_written"] == len(events)

    def test_the_parameters_are_summarised_not_dumped(self, tmp_path) -> None:
        """载荷可能有几百 KB，参数也可能很长 —— 会话里只留摘要。"""
        _result, _ctx = _run(session=str(tmp_path))
        events = read_session(tmp_path / "run-session-1.jsonl")
        params = next(event["params"] for event in events if event["kind"] == "tool_call")
        assert params["service"] == "order-service", "名字类参数是调查的关键上下文"
        assert params["start"] and params["end"]
        assert all(isinstance(value, (str, int, float, bool)) for value in params.values())

    def test_the_start_event_does_not_quote_the_alert_body(self, tmp_path) -> None:
        """告警原文可能很长、也可能是注入内容，会话只记"有没有"。"""
        _result, _ctx = _run(session=str(tmp_path))
        start = read_session(tmp_path / "run-session-1.jsonl")[0]
        assert start["alert_summary_present"] is True
        assert "checkout p99" not in json.dumps(start, ensure_ascii=False)


class TestItCannotFillTheDisk:
    def test_the_recorder_stops_at_the_cap_and_says_so(self, tmp_path) -> None:
        recorder = recorder_for("run-cap", directory=tmp_path)
        with recorder:
            for index in range(MAX_EVENTS + 5):
                recorder.record("tool_call", tool=f"t{index}", ok=True)
        events = read_session(tmp_path / "run-cap.jsonl")
        assert len(events) == MAX_EVENTS + 1, "上限之后只留一条 truncated 说明"
        assert events[-1]["kind"] == "session.truncated"
        assert str(MAX_EVENTS) in events[-1]["reason"]

    def test_a_bad_run_id_cannot_escape_the_directory(self, tmp_path) -> None:
        recorder = recorder_for("../../escape", directory=tmp_path)
        assert recorder.path is not None
        assert recorder.path.parent == tmp_path, "run id 里的 ../ 不能拼出目录外的路径"
        assert "/" not in recorder.path.name
        # 清洗后什么都不剩的名字必须报错，而不是静默写一个叫 "-" 的文件
        import pytest

        with pytest.raises(ValueError):
            safe_name("///")

    def test_recording_failures_do_not_break_the_investigation(self) -> None:
        """写不进去（目录不可写）时调查照常完成，只是没有会话文件。"""
        recorder = SessionRecorder(path=None, run_id="run-x")
        recorder.record("node", node="plan")  # no-op，不抛
        assert recorder.enabled is False
        assert recorder.written == 0
