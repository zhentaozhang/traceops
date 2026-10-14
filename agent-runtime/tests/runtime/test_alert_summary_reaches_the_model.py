"""告警原文必须真的到达模型。

`ModelRequest.alert_summary` 这个字段在仓库里存在了很久，注释也写着
"规则路径不读它，只透传给模型 —— 这让'能否利用自然语言线索'成为可测的
差异来源"。评测里 F-011 场景正是靠一句话线索才能定位根因。

但生产路径上它从来没有值：
Java 的 `EventMessages.investigationRequested` 不写这个字段（契约里也没有
这个字段），Python 的 `derive_request_from_event` 不读它。于是
`payload.get("alert_summary")` 永远拿到 None，而评测 harness 自己
直接塞了 `scenario.alert_summary` —— 评测能测出差异，生产里这条线索
一次都没用上。

这个文件盯住三件事：契约里有它、派生时有它、模型请求里有它。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetManager
from traceops.runtime.model import ModelGateway, ScriptedModel
from traceops.runtime.worker import derive_request_from_event

SUMMARY = "checkout latency spike traced to cache tier: zulu-cache responses degraded"
EVENT_ID = "evt-11111111-1111-4111-8111-111111111111"


def requested_event(*, alert_summary: str | None = SUMMARY) -> dict:
    payload = {
        "investigation_run_id": "run-001",
        "incident_id": "inc-001",
        "service_hint": "order-service",
        "environment": "demo",
        "time_range": {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:06:00Z"},
        "config_version": "cfg",
        "priority": "high",
    }
    if alert_summary is not None:
        payload["alert_summary"] = alert_summary
    return {
        "envelope": {
            "event_id": EVENT_ID,
            "event_type": "investigation.requested",
            "schema_version": "1.0",
            "occurred_at": "2026-10-30T10:06:00Z",
            "correlation_id": "inc-001",
            "causation_id": None,
            "aggregate_id": "run-001",
            "aggregate_version": 1,
            "producer": "control-plane",
        },
        "payload": payload,
    }


class TestDerivationKeepsTheSummary:
    def test_the_summary_survives_derivation(self):
        request = derive_request_from_event(requested_event())
        assert request.alert_summary == SUMMARY

    def test_absent_summary_stays_none_rather_than_empty_string(self):
        """None 与 "" 必须能区分：前者是"没有线索"，后者是"有线索且为空"。"""
        request = derive_request_from_event(requested_event(alert_summary=None))
        assert request.alert_summary is None


class _Adapters:
    """最小可用的数据源替身：产出三条能通过归一化的证据。

    必须真的产出证据 —— `_hypothesize_node` 只在有证据时才问模型
    （没有证据就没有可讨论的对象）。用一个空上下文测"模型收到了什么"，
    测到的永远是"模型没被调用"。
    """

    @staticmethod
    def _at(start: str) -> int:
        """采样点必须落在请求窗口内 —— 落在窗口外会被 Evidence 的不变量拒绝，
        那时测的就不是"模型收到了什么"，而是"证据为什么没了"。"""
        from datetime import datetime
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
            "values": [[str(moment * 1_000_000_000),
                        "ERROR redis timeout - pool exhausted"]],
        }]}}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        moment = self._at(start)
        return {"traces_considered": 1, "batches": [{
            "resource": {"service.name": service},
            "spans": [{"traceID": "a" * 32, "spanID": "1" * 16,
                       "operationName": "redis.command",
                       "startTime": moment * 1_000_000, "duration": 2_000_000}]}]}


class TestTheModelActuallySeesIt:
    def _run(self, *, alert_summary: str | None):
        from traceops.evidence.store import EvidenceStore
        from traceops.tools.gateway import ToolGateway, register_builtin_tools

        model = ScriptedModel()
        budget = BudgetManager(max_tool_calls=5, deadline_seconds=60)
        gateway = ToolGateway()
        register_builtin_tools(gateway, _Adapters())
        request = InvestigationRequest(
            investigation_run_id="run-001",
            incident_id="inc-001",
            service_hint="order-service",
            fault_start=datetime(2026, 10, 30, 10, 5, tzinfo=UTC),
            window_start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
            window_end=datetime(2026, 10, 30, 10, 6, tzinfo=UTC),
            alert_summary=alert_summary,
        )
        context = RunContext(
            store=EvidenceStore(),
            adapters=None,
            budget=budget,
            gateway=gateway,
            model=ModelGateway(model, budget),
        )
        run_investigation(request, context)
        return model

    def test_the_model_request_carries_the_alert_text(self):
        model = self._run(alert_summary=SUMMARY)
        assert model.calls, "模型没有被调用，这个测试就没证明任何事"
        assert model.calls[0].alert_summary == SUMMARY

    def test_without_a_summary_the_model_is_told_nothing(self):
        model = self._run(alert_summary=None)
        assert model.calls
        assert model.calls[0].alert_summary is None


class TestPrioritySpeaksOneVocabulary:
    """三套优先级词汇曾经并存：契约 low|normal|high、Python 默认 "P2"、
    Java 人工入口写 "manual"、评测 fixture 写 "P1"。
    一个没人读的字段用哪套词都不会报错，所以它们并存了很久。"""

    def test_the_default_is_a_contract_value(self):
        request = InvestigationRequest(
            investigation_run_id="r", incident_id="i", service_hint="s",
            fault_start=datetime(2026, 10, 30, 10, 5, tzinfo=UTC),
            window_start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
            window_end=datetime(2026, 10, 30, 10, 6, tzinfo=UTC),
        )
        assert request.priority in ("low", "normal", "high")

    def test_an_invented_priority_is_rejected(self):
        with pytest.raises(Exception) as excinfo:
            InvestigationRequest(
                investigation_run_id="r", incident_id="i", service_hint="s",
                fault_start=datetime(2026, 10, 30, 10, 5, tzinfo=UTC),
                window_start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
                window_end=datetime(2026, 10, 30, 10, 6, tzinfo=UTC),
                priority="P1",
            )
        assert "priority" in str(excinfo.value)
