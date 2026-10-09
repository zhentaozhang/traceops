"""harness 必须区分「这个场景没声明这类信号」和「查询失败了」。

## 为什么

场景 JSON 只声明**它关心的**信号。比如 F-011 只给 `zulu-cache` 声明了
慢 span，`alpha-payment` 和 `api-gateway` 一条都没声明。这是场景设计的
正常状态，不是错误 —— 声明"这个服务没有慢 span"和"这个服务有慢 span"
是同一件事的两面。

但 harness 之前用**抛异常**表达这件事：

```python
raise ScenarioDataUnavailable(f"{scenario.scenario_id}: no slow spans declared for {service}")
```

这个异常一路冒到 `graph._collect_node` 的 `except Exception`，后果是：

1. **stderr 打出 126 个完整堆栈**（一次评估跑 12 个场景）——
   真正的失败会被这堆噪音淹掉；
2. 审计 outcome 记成 **ERROR**，而真实语义是"查过了，确实没有"；
   这正是 `test_gateway_no_data.py` 明确禁止的那种混淆
   （"查了没有" ≠ "没法查"）；
3. `missing_data` 里写的是 "query_service_traces: F-011: no slow spans
   declared for api-gateway" —— 一条**正常**的观测被记成"数据缺失"。

第 2 条最严重：ERROR 的语义是"禁止 REFUTED"（我们什么都没看见），
NO_DATA 的语义是"可以支持 REFUTED"（查过了，确实没有）。两者的结论
方向正好相反。

## 正确的形状

不抛异常，返回**数据源正常响应但结果为空**的那种 payload ——
和真实 Prometheus/Loki/Tempo 在"查到了但是空的"时候返回的形状一样。
这样 gateway 自己就会判成 NO_DATA，证据层按"空但合法"处理，
missing_data 里也不会出现它。

那"真的查不了"怎么表达？让适配器**真的失败**（抛异常）——
那是 ERROR 路径，已经有了。
"""
from __future__ import annotations

import pytest

from evaluation.harness import (
    ScenarioAdapters,
    error_rate_payload,
    logs_payload,
    traces_payload,
)
from evaluation.scenario import load_scenario
from pathlib import Path

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


def _load(scenario_id: str):
    return load_scenario(SCENARIOS_DIR / f"{scenario_id}.json")


class TestEmptyIsNotAnError:
    def test_a_service_without_declared_spans_yields_an_empty_payload(self):
        """没有声明慢 span 的服务 → 返回**合法但空**的 payload，不抛异常。"""
        scenario = _load("F-011")
        adapters = ScenarioAdapters(scenario)

        # alpha-payment 声明了错误率序列，但**没有**声明慢 span
        payload = adapters.query_service_traces(
            service="alpha-payment", start="2026-10-30T10:00:00Z", end="2026-10-30T10:05:00Z")

        # Tempo 的空响应形状和 Prometheus/Loki 不同：没有 status 字段，
        # 而是以 traces_considered=0 表示"查过了，一条都没有"。
        # 这正是 gateway `_is_empty_result` 认得的第三种空形状。
        assert payload["traces_considered"] == 0
        assert payload["batches"] == []

    def test_an_empty_metric_payload_has_the_prometheus_shape(self):
        """空指标结果必须是 Prometheus 的形状，不能是别的。

        形状不对的话，normalizer 会抛 ValueError，然后又被当成 ERROR ——
        绕一圈又回到原来的问题。
        """
        # F-001 没有 per_service，顶层信号只属于 service_hint（order-service）。
        # 拓扑里的下游 redis 因此没有任何错误率序列 —— 这正是"查了，没有"。
        scenario = _load("F-001")
        payload = error_rate_payload(scenario, "redis")
        assert payload["status"] == "success"
        assert payload["data"]["resultType"] == "matrix"
        assert payload["data"]["result"] == []

    def test_an_empty_log_payload_has_the_loki_shape(self):
        scenario = _load("F-011")
        # F-011 的所有服务都没声明错误日志
        payload = logs_payload(scenario, "alpha-payment")
        assert payload["status"] == "success"
        assert payload["data"]["resultType"] == "streams"
        assert payload["data"]["result"] == []

    def test_declared_data_is_still_returned(self):
        """有声明的服务照常返回数据 —— 不能为了"不抛异常"把真数据也吞了。"""
        scenario = _load("F-011")
        payload = traces_payload(scenario, "zulu-cache")
        assert payload["batches"], "zulu-cache 声明了慢 span，必须有内容"

        metrics = error_rate_payload(scenario, "alpha-payment")
        assert metrics["data"]["result"], "alpha-payment 声明了错误率序列，必须有内容"


class TestTheGatewaySeesNoData:
    def test_an_undeclared_service_is_audited_as_no_data_not_error(self):
        """这是整件事的核心：审计 outcome 必须是 NO_DATA，不是 ERROR。"""
        from traceops.runtime.budget import BudgetManager
        from traceops.tools.gateway import ToolGateway, ToolPermission, ToolSpec

        scenario = _load("F-011")
        adapters = ScenarioAdapters(scenario)

        gw = ToolGateway()
        gw.register(ToolSpec(
            name="query_service_traces",
            description="traces",
            input_schema={"type": "object", "properties": {
                "service": {"type": "string"}, "start": {"type": "string"},
                "end": {"type": "string"}},
                "required": ["service", "start", "end"],
                "additionalProperties": False},
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=adapters.query_service_traces,
        ))

        gw.invoke("query_service_traces",
                  {"service": "alpha-payment",
                   "start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:05:00Z"},
                  budget=BudgetManager())

        outcomes = [a["outcome"] for a in gw.audit_log()]
        assert outcomes == ["NO_DATA"], f"期望 NO_DATA，实际 {outcomes}"


class TestMissingDataStaysClean:
    def test_an_undeclared_service_is_not_reported_as_missing_data(self):
        """未声明的服务不该出现在 missing_data 里。

        它不是"数据缺失"，是"这个场景没这个信号"。把它写进 missing_data
        会让报告里出现一句并不存在的警告（"查不到 X"），
        也让运维误以为数据源出问题了。
        """
        from traceops.evidence.store import EvidenceStore
        from traceops.orchestration.graph import RunContext, run_investigation
        from traceops.runtime.budget import BudgetManager
        from traceops.topology.graph import ServiceTopology
        from evaluation.harness import build_request

        scenario = _load("F-011")
        topology = ServiceTopology.from_edges(
            [(e[0], e[1]) for e in scenario.topology_edges])
        ctx = RunContext(
            store=EvidenceStore(),
            adapters=ScenarioAdapters(scenario),
            budget=BudgetManager(max_tool_calls=40),
            topology=topology,
            scope_depth=scenario.scope_depth,
            model=None,
        )
        result = run_investigation(build_request(scenario), ctx)

        offenders = [m for m in result.report.missing_data if "no " in m and "declared" in m]
        assert not offenders, f"未声明的服务不该进 missing_data：{offenders}"
