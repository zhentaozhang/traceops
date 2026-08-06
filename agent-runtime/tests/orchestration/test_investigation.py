"""LangGraph 调查编排测试（Cycle B）。

覆盖：
- 端到端：故障窗口 + 数据源适配器 → 证据 → 假设 → 规则排序 → 结构化报告
- 无数据：INCONCLUSIVE + abstained + missing_data
- 预算耗尽：FAILED，不留半成品报告
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import EvidenceType
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetManager

FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)


class ScriptedAdapters:
    """四个内置工具的最小可编程实现（返回数据源原生 payload）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error_rate_payload: dict | None = None
        self.logs_payload: dict | None = None
        self.traces_payload: dict | None = None
        self.raise_on: set[str] = set()

    def query_service_error_rate(self, service, start, end):
        self.calls.append("error_rate")
        if "error_rate" in self.raise_on:
            raise RuntimeError("prometheus down")
        return self.error_rate_payload or {
            "status": "success",
            "data": {"resultType": "matrix", "result": []},
        }

    def query_service_logs(self, service, start, end, filter=None):
        self.calls.append("logs")
        if "logs" in self.raise_on:
            raise RuntimeError("loki down")
        return self.logs_payload or {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        self.calls.append("traces")
        if "traces" in self.raise_on:
            raise RuntimeError("tempo down")
        return self.traces_payload or {"traces_considered": 0, "batches": []}

    def query_metric_series(self, service, metric, start, end):
        self.calls.append("metric_series")
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}


def _redis_pool_payloads() -> dict[str, dict]:
    ts = int(FAULT_START.timestamp())
    return {
        "error_rate": {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"service": "order-service"},
                        "values": [[ts - 60, "0.01"], [ts, "0.42"]],
                    }
                ],
            },
        },
        "logs": {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"service": "order-service", "level": "error"},
                        "values": [
                            [str(ts * 1_000_000_000), "ERROR redis timeout after 2000ms: pool exhausted"]
                        ],
                    }
                ],
            },
        },
        "traces": {
            "traces_considered": 1,
            "batches": [
                {
                    "resource": {"service.name": "order-service"},
                    "spans": [
                        {
                            "traceID": "a" * 32,
                            "spanID": "1111111111111111",
                            "operationName": "redis.command",
                            "startTime": ts * 1_000_000,
                            "duration": 2_000_000,
                        }
                    ],
                }
            ],
        },
    }


def _request(**overrides) -> InvestigationRequest:
    payload = {
        "investigation_run_id": "run-001",
        "incident_id": "inc-001",
        "environment": "demo",
        "service_hint": "order-service",
        "fault_start": FAULT_START,
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
    }
    payload.update(overrides)
    return InvestigationRequest(**payload)


def _context(adapters, *, budget: BudgetManager | None = None) -> RunContext:
    return RunContext(
        store=EvidenceStore(),
        gateway=None,  # run_investigation 内部构建 gateway
        budget=budget or BudgetManager(),
        adapters=adapters,
    )


def test_e2e_tie_is_reported_as_ambiguous_not_overclaimed() -> None:
    """三条证据各生成一个候选、得分相同 → 必须弃权（歧义），不能任选一个当根因。"""
    payloads = _redis_pool_payloads()
    adapters = ScriptedAdapters()
    adapters.error_rate_payload = payloads["error_rate"]
    adapters.logs_payload = payloads["logs"]
    adapters.traces_payload = payloads["traces"]
    ctx = _context(adapters)

    result = run_investigation(_request(), ctx)

    assert result.status == "INCONCLUSIVE"
    report = result.result if hasattr(result, "result") else result.report
    assert report is not None
    assert report.diagnosis_status == "INCONCLUSIVE"
    assert report.abstained is True
    assert "Ambiguous evidence" in report.summary

    # 关键语义：日志与链路提到 redis，所以它们必须被归因为"关于 redis"的证据，
    # 而不是"order-service 自己拥有一个 redis 池"。因此 redis 的候选排在症状服务之前。
    services = [h.candidate_service for h in report.hypotheses]
    assert services[0] == "redis", services
    assert services.index("redis") < services.index("order-service"), services

    # 至少存在一个"pool_exhaustion"候选，且归因到 redis 的连接池
    pool = next((h for h in report.hypotheses if h.fault_type == "pool_exhaustion"), None)
    assert pool is not None
    assert pool.candidate_service == "redis"
    assert pool.candidate_component == "redis-connection-pool"
    # 证据引用完整性：报告里每条证据都能在 Store 中校验通过
    assert len(report.all_evidence_ids()) >= 3
    for evidence_id in report.all_evidence_ids():
        validation = ctx.store.validate(
            evidence_id,
            incident_id="inc-001",
            investigation_window=(WINDOW_START, WINDOW_END),
        )
        assert validation.passed, validation.errors
    # 工具确实被调用（含审计）
    assert {"error_rate", "logs", "traces"} <= set(adapters.calls)
    assert ctx.budget.tool_calls == 3


def test_no_data_yields_inconclusive_report() -> None:
    ctx = _context(ScriptedAdapters())

    result = run_investigation(_request(), ctx)

    assert result.status == "INCONCLUSIVE"
    assert result.report is not None
    assert result.report.abstained is True
    assert result.report.missing_data, "INCONCLUSIVE must state what is missing"
    assert result.errors == []


def test_budget_exhaustion_fails_without_report() -> None:
    adapters = ScriptedAdapters()
    adapters.error_rate_payload = _redis_pool_payloads()["error_rate"]
    ctx = _context(adapters, budget=BudgetManager(max_tool_calls=1))

    result = run_investigation(_request(), ctx)

    assert result.status == "FAILED"
    assert result.report is None
    assert any("budget" in e.lower() for e in result.errors)


def test_request_validation_rejects_inverted_window() -> None:
    with pytest.raises(ValueError):
        _request(window_start=WINDOW_END, window_end=WINDOW_START)


def test_config_change_event_feeds_ranking() -> None:
    payloads = _redis_pool_payloads()
    adapters = ScriptedAdapters()
    adapters.error_rate_payload = payloads["error_rate"]
    adapters.logs_payload = payloads["logs"]
    adapters.traces_payload = payloads["traces"]
    ctx = _context(adapters)
    request = _request(
        window_start=FAULT_START - timedelta(minutes=5),
        config_change={
            "changed_at": (FAULT_START - timedelta(minutes=3)).isoformat(),
            "component": "redis-connection-pool",
            "summary": "maxPoolSize reduced 20 -> 2",
        },
    )

    result = run_investigation(request, ctx)

    assert result.status == "SUPPORTED"
    assert result.report is not None
    # R-004：组件匹配的配置变更让 pool_exhaustion 升到第 1
    assert result.report.hypotheses[0].rank == 1
    assert result.report.hypotheses[0].fault_type == "pool_exhaustion"
    # 配置变更事件必须作为证据出现在报告里（R-004 的排序输入）
    assert any(e.evidence_type == EvidenceType.EVENT for e in result.report.evidence)
