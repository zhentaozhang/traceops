"""把 Scenario 变成一次真实调查，并按 ground truth 打分。

harness 只做三件事：
1. 按 signals 生成 Prometheus / Loki / Tempo 形状的 payload（合成数据）
2. 交给 agent-runtime 的 run_investigation 跑完整编排
3. 用 metrics.score_scenario 打分

它**不**伪造成功：数据源返回空/异常时适配器抛错，编排层把该数据源记进
missing_data，结论可以是 INCONCLUSIVE —— 这正是要评估的行为。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from evaluation.metrics import ScenarioOutcome, score_scenario
from evaluation.scenario import Scenario
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest,
    RunContext,
    run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

STEP_SECONDS = 60
MAX_TOOL_CALLS = 10


class ScenarioDataUnavailable(RuntimeError):
    """该场景没有声明这类信号（模拟"数据源里查不到数据"）。"""


def _epoch(value: datetime) -> float:
    return value.timestamp()


def _series_for(scenario: Scenario, service: str) -> list[float]:
    if scenario.signals.per_service:
        entry = scenario.signals.per_service.get(service)
        return list(entry.error_rate_series) if entry else []
    if service != scenario.service_hint:
        return []
    return list(scenario.signals.error_rate_series)


def _logs_for(scenario: Scenario, service: str) -> list[str]:
    if scenario.signals.per_service:
        entry = scenario.signals.per_service.get(service)
        return list(entry.error_logs) if entry else []
    if service != scenario.service_hint:
        return []
    return list(scenario.signals.error_logs)


def _spans_for(scenario: Scenario, service: str) -> tuple[list, int]:
    if scenario.signals.per_service:
        entry = scenario.signals.per_service.get(service)
        if not entry:
            return [], 0
        return list(entry.slow_spans), entry.span_offset_ms
    if service != scenario.service_hint:
        return [], 0
    return list(scenario.signals.slow_spans), 0


def error_rate_payload(scenario: Scenario, service: str | None = None) -> dict[str, Any]:
    service = service or scenario.service_hint
    series = _series_for(scenario, service)
    if not series:
        raise ScenarioDataUnavailable(
            f"{scenario.scenario_id}: no error-rate series declared for {service}"
        )
    last = _epoch(scenario.fault_start)
    values = [[last - STEP_SECONDS * (len(series) - 1 - i), float(v)] for i, v in enumerate(series)]
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"service": service}, "values": values}],
        },
    }


def logs_payload(scenario: Scenario, service: str | None = None) -> dict[str, Any]:
    service = service or scenario.service_hint
    messages = _logs_for(scenario, service)
    if not messages:
        raise ScenarioDataUnavailable(
            f"{scenario.scenario_id}: no error logs declared for {service}"
        )
    base_ns = int(_epoch(scenario.fault_start) * 1_000_000_000)
    values = [[str(base_ns + i * 1_000_000_000), message] for i, message in enumerate(messages)]
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service": service, "level": "error"},
                    "values": values,
                }
            ],
        },
    }


def traces_payload(scenario: Scenario, service: str | None = None) -> dict[str, Any]:
    service = service or scenario.service_hint
    spans, offset_ms = _spans_for(scenario, service)
    if not spans:
        raise ScenarioDataUnavailable(
            f"{scenario.scenario_id}: no slow spans declared for {service}"
        )
    start_us = int(_epoch(scenario.fault_start) * 1_000_000) + offset_ms * 1000
    tempo_spans = [
        {
            "traceID": f"{scenario.scenario_id.lower().replace('-', '')}00{i:02d}",
            "spanID": f"{i:016x}",
            "operationName": span.name,
            "startTime": start_us,
            "duration": int(span.duration_ms * 1000),
            "tags": [{"key": "error", "value": False}],
        }
        for i, span in enumerate(spans)
    ]
    return {
        "traces_considered": 1,
        "batches": [
            {
                "resource": {"service.name": service},
                "spans": tempo_spans,
            }
        ],
    }


class ScenarioAdapters:
    """实现 register_builtin_tools 需要的四个只读方法。"""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.calls: list[tuple[str, str]] = []

    def query_metric_series(self, *, service: str, metric: str, start: str, end: str) -> dict[str, Any]:
        self.calls.append(("query_metric_series", service))
        return error_rate_payload(self.scenario, service)

    def query_service_error_rate(self, *, service: str, start: str, end: str) -> dict[str, Any]:
        self.calls.append(("query_service_error_rate", service))
        return error_rate_payload(self.scenario, service)

    def query_service_logs(
        self, *, service: str, start: str, end: str, filter: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("query_service_logs", service))
        return logs_payload(self.scenario, service)

    def query_service_traces(
        self, *, service: str, start: str, end: str, min_duration_ms: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(("query_service_traces", service))
        return traces_payload(self.scenario, service)


def build_request(scenario: Scenario) -> InvestigationRequest:
    config_change = (
        scenario.signals.config_change.model_dump(mode="json")
        if scenario.signals.config_change is not None
        else None
    )
    return InvestigationRequest(
        investigation_run_id=f"run-{scenario.scenario_id}",
        incident_id=f"inc-{scenario.scenario_id}",
        environment="eval-synthetic",
        service_hint=scenario.service_hint,
        fault_start=scenario.fault_start,
        window_start=scenario.window_start,
        window_end=scenario.window_end,
        config_change=config_change,
        priority="P1",
    )


def run_scenario(scenario: Scenario) -> ScenarioOutcome:
    """执行一次调查并打分。每次运行使用全新的 store / budget，互不污染。"""
    store = EvidenceStore()
    adapters = ScenarioAdapters(scenario)
    topology = (
        ServiceTopology.from_edges([(edge[0], edge[1]) for edge in scenario.topology_edges])
        if scenario.topology_edges
        else None
    )
    ctx = RunContext(
        store=store,
        adapters=adapters,
        budget=BudgetManager(max_tool_calls=MAX_TOOL_CALLS),
        topology=topology,
        scope_depth=scenario.scope_depth,
    )
    result = run_investigation(build_request(scenario), ctx)
    return score_scenario(scenario, result)
