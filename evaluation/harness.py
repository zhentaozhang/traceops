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

from evaluation.investigation_profile import DEFAULT_PROFILE
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
# 每次调查的查询计划随拓扑扩大：单服务 = 3 次查询；两层采集（调查层 + 影响层）
# 在 3 服务场景下需要约 9~12 次。预算必须覆盖完整计划，否则会静默截断采集、
# 导致传播链天然不完整——这是之前 chain=0.00 的一个隐藏成因。
MAX_TOOL_CALLS = 40


class ScenarioDataUnavailable(RuntimeError):
    """该场景没有声明这类信号（模拟"数据源里查不到数据"）。

    .. deprecated::
        **不再用于表达"场景没声明这类信号"**。那件事现在返回合法的空
        payload（见 `_empty_*_payload`），因为它是"查了，没有"，不是
        "没法查"。保留这个类型只为兼容既有引用；新代码不该再抛它。

    真正需要抛的场景是"查询本身失败了"（比如数据源挂了）—— 那种情况
    走普通异常路径，gateway 会记成 ERROR。
    """


def _empty_metric_payload(service: str) -> dict[str, Any]:
    """Prometheus 的"查到了，但是空的"响应。

    形状必须和真实 Prometheus 一致（`resultType: matrix` + 空 `result`），
    否则 normalizer 会抛 ValueError，绕一圈又变成 ERROR。
    """
    return {
        "status": "success",
        "data": {"resultType": "matrix", "result": []},
    }


def _empty_log_payload(service: str) -> dict[str, Any]:
    """Loki 的"查到了，但是空的"响应（`resultType: streams` + 空 `result`）。"""
    return {
        "status": "success",
        "data": {"resultType": "streams", "result": []},
    }


def _empty_trace_payload() -> dict[str, Any]:
    """Tempo 的"查到了，但是空的"响应（`traces_considered: 0` + 空 `batches`）。

    这正是 gateway `_is_empty_result` 认得的第三种空形状。
    """
    return {"traces_considered": 0, "batches": []}


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
        # **空结果不是错误**。场景没为这个服务声明错误率序列 = 这个服务
        # 在这个时间窗里没有错误率数据 —— 和真实 Prometheus 返回空 result
        # 是同一件事。抛异常会让 gateway 记成 ERROR（"没法查"），
        # 而语义是 NO_DATA（"查了，没有"）—— 两者的结论方向正好相反。
        return _empty_metric_payload(service)
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
        return _empty_log_payload(service)
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
        return _empty_trace_payload()
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
        # 契约里只有 low|normal|high 三档。这里曾经写 "P1" —— 评测自己
        # 用了一套词汇，于是"评测里能跑通"并不代表"生产契约能接受"。
        priority="high",
        alert_summary=scenario.alert_summary,
    )


def run_scenario(scenario: Scenario, *, model=None, profile=None) -> ScenarioOutcome:
    """执行一次调查并打分。每次运行使用全新的 store / budget，互不污染。

    `model` 传入时启用 LLM 假设生成（E1/E2 消融臂需要它；
    不传则走纯确定性路径，即 E3/E4/E5）。

    `profile` 传入时把该臂的策略**真的**应用到这个 RunContext 上。
    必须真的应用：算一个 digest 却只拿去打印、不传给运行，等于三个臂
    跑的是同一份配置 —— 消融会得出"三臂结果相同"，而真相是
    "三臂根本没跑不同的东西"。这种失败比"测不出差异"更危险：
    它把"没接线"包装成"消融完成、E5 无增益"。
    """
    store = EvidenceStore()
    adapters = ScenarioAdapters(scenario)
    topology = (
        ServiceTopology.from_edges([(edge[0], edge[1]) for edge in scenario.topology_edges])
        if scenario.topology_edges
        else None
    )
    # 不传 profile 时用 DEFAULT_PROFILE —— 它就是当前代码行为，
    # 由 test_investigation_profile 逐项比对代码常量，所以默认不变。
    effective = profile or DEFAULT_PROFILE
    ctx = RunContext(
        store=store,
        adapters=adapters,
        budget=BudgetManager(max_tool_calls=MAX_TOOL_CALLS),
        topology=topology,
        scope_depth=scenario.scope_depth,
        model=model,
        # E4 vs E5 的唯一差别：假设验证的两项能力
        enable_semantic_gate=effective.enable_semantic_gate,
        enable_contradiction=effective.enable_contradiction,
    )
    result = run_investigation(build_request(scenario), ctx)
    # 把本次**实际使用**的策略 digest 记进报告：两次调查同一事故结论不同时，
    # 要能回答"是策略变了还是数据变了"。
    if result.report is not None:
        result.report.profile_digest = effective.digest()
    return score_scenario(scenario, result)
