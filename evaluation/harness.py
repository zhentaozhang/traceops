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

STEP_SECONDS = 60
MAX_TOOL_CALLS = 10


class ScenarioDataUnavailable(RuntimeError):
    """该场景没有声明这类信号（模拟"数据源里查不到数据"）。"""


def _epoch(value: datetime) -> float:
    return value.timestamp()


def error_rate_payload(scenario: Scenario) -> dict[str, Any]:
    series = scenario.signals.error_rate_series
    if not series:
        raise ScenarioDataUnavailable(
            f"{scenario.scenario_id}: no error-rate series declared for {scenario.service_hint}"
        )
    last = _epoch(scenario.fault_start)
    values = [[last - STEP_SECONDS * (len(series) - 1 - i), float(v)] for i, v in enumerate(series)]
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {"service": scenario.service_hint}, "values": values}],
        },
    }


def logs_payload(scenario: Scenario) -> dict[str, Any]:
    messages = scenario.signals.error_logs
    if not messages:
        raise ScenarioDataUnavailable(
            f"{scenario.scenario_id}: no error logs declared for {scenario.service_hint}"
        )
    base_ns = int(_epoch(scenario.fault_start) * 1_000_000_000)
    values = [[str(base_ns + i * 1_000_000_000), message] for i, message in enumerate(messages)]
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service": scenario.service_hint, "level": "error"},
                    "values": values,
                }
            ],
        },
    }


def traces_payload(scenario: Scenario) -> dict[str, Any]:
    spans = scenario.signals.slow_spans
    if not spans:
        raise ScenarioDataUnavailable(
            f"{scenario.scenario_id}: no slow spans declared for {scenario.service_hint}"
        )
    start_us = int(_epoch(scenario.fault_start) * 1_000_000)
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
                "resource": {"service.name": scenario.service_hint},
                "spans": tempo_spans,
            }
        ],
    }


class ScenarioAdapters:
    """实现 register_builtin_tools 需要的四个只读方法。"""

    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.calls: list[str] = []

    def query_metric_series(self, *, service: str, metric: str, start: str, end: str) -> dict[str, Any]:
        self.calls.append("query_metric_series")
        if not self.scenario.signals.error_rate_series:
            raise ScenarioDataUnavailable(f"{self.scenario.scenario_id}: no metric data")
        return error_rate_payload(self.scenario)

    def query_service_error_rate(self, *, service: str, start: str, end: str) -> dict[str, Any]:
        self.calls.append("query_service_error_rate")
        return error_rate_payload(self.scenario)

    def query_service_logs(
        self, *, service: str, start: str, end: str, filter: str | None = None
    ) -> dict[str, Any]:
        self.calls.append("query_service_logs")
        return logs_payload(self.scenario)

    def query_service_traces(
        self, *, service: str, start: str, end: str, min_duration_ms: int | None = None
    ) -> dict[str, Any]:
        self.calls.append("query_service_traces")
        return traces_payload(self.scenario)


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
    ctx = RunContext(
        store=store,
        adapters=adapters,
        budget=BudgetManager(max_tool_calls=MAX_TOOL_CALLS),
    )
    result = run_investigation(build_request(scenario), ctx)
    return score_scenario(scenario, result)
