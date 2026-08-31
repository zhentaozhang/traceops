"""影响范围必须有观测证据，不能只靠拓扑推理。

## 为什么必须改（aurora-lab E-1 的规则化版本）

`_affected_services` 现在是：

```python
radius = topology.blast_radius(lead.candidate_service, depth=2)
return sorted(radius)          # ← 纯拓扑推理
```

拓扑里有依赖 ≠ 该服务此刻在受苦。aurora-lab 场景 12 的失败正是这个形状：
报告说「其它 workload 都健康、未受影响」，唯一依据是 pod status，
从没查过它们的流量/错误率 —— 真实第二故障就这么漏掉，而且报告还说它不存在。

我们的版本更隐蔽：不是"没查就宣布健康"，而是**把从未观测过的服务写进影响范围**。
两者都违反了同一条纪律：**"health / unaffected" 的主张需要流量级证据**。

## 正确行为

影响范围只收录**采集层真的拿到异常证据**的服务。拿不到证据的服务不写进去，
而是记进一个「拓扑上相关但未观测」的清单 —— 明确说"没查"，而不是默认"没事"。
"""
from __future__ import annotations

import pytest

from traceops.orchestration.graph import (
    InvestigationRequest, RunContext, run_investigation,
)
from traceops.evidence.store import EvidenceStore
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology
from datetime import UTC, datetime, timedelta

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


class ScriptedAdapters:
    """三服务拓扑：api-gateway → order-service → redis。"""

    def __init__(self, *, order_has_evidence: bool, order_is_anomalous: bool = False) -> None:
        self.order_has_evidence = order_has_evidence
        self.order_is_anomalous = order_is_anomalous
        self.calls: list[tuple[str, dict]] = []

    def query_service_error_rate(self, service, start, end):
        self.calls.append(("error_rate", {"service": service}))
        if service == "order-service":
            if not self.order_has_evidence:
                # 查询成功但结果为空 = 什么都没看见。
                return {"status": "success", "data": {"resultType": "matrix", "result": []}}
            level = "0.4" if self.order_is_anomalous else "0.01"
            return {"status": "success", "data": {"resultType": "matrix", "result": [
                {"metric": {"service": service}, "values": [[TS.timestamp(), level]]}]}}
        if service == "redis":
            return {"status": "success", "data": {"resultType": "matrix", "result": [
                {"metric": {"service": service}, "values": [[TS.timestamp(), "0.4"]]}]}}
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    def query_service_logs(self, service, start, end, filter=None):
        self.calls.append(("logs", {"service": service}))
        if service == "redis":
            return {"status": "success", "data": {"resultType": "streams", "result": [
                {"stream": {"service": service}, "values": [
                    [str(int(TS.timestamp() * 1e9)), "ERROR redis timeout pool exhausted"]]}]}}
        raise ValueError(f"no logs for {service}")

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        self.calls.append(("traces", {"service": service}))
        raise ValueError(f"no traces for {service}")


def _run(*, order_has_evidence: bool, order_is_anomalous: bool = False):
    topology = ServiceTopology.from_edges([
        ("api-gateway", "order-service"),
        ("order-service", "redis"),
    ])
    adapters = ScriptedAdapters(
        order_has_evidence=order_has_evidence, order_is_anomalous=order_is_anomalous)
    store = EvidenceStore()
    ctx = RunContext(
        store=store, adapters=adapters,
        budget=BudgetManager(max_tool_calls=40),
        topology=topology, scope_depth=2,
    )
    request = InvestigationRequest(
        investigation_run_id="run-affect", incident_id="inc-affect",
        environment="demo", service_hint="order-service",
        fault_start=TS, window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
    )
    return run_investigation(request, ctx)


class TestAffectedRequiresEvidence:
    def test_unobserved_upstream_is_not_listed_as_affected(self):
        """api-gateway 在拓扑上游，但没采到它的异常 → 不能写进影响范围。"""
        result = _run(order_has_evidence=False)
        report = result.report
        assert report is not None
        # api-gateway 只在拓扑里，没被观测到
        assert "api-gateway" not in report.affected_services, (
            "unaffected claim invented from topology alone")

    def test_unobserved_upstream_is_recorded_as_not_checked(self):
        """没观测到的相关服务必须显式记为"未检查"，而不是静默省略。"""
        result = _run(order_has_evidence=False)
        report = result.report
        text = " ".join(report.missing_data)
        assert "api-gateway" in text, (
            f"expected api-gateway listed as unchecked, got missing_data={report.missing_data}")

    def test_observed_anomalous_upstream_is_affected(self):
        """采到**异常**证据的上游服务才进影响范围。"""
        result = _run(order_has_evidence=True, order_is_anomalous=True)
        assert "order-service" in result.report.affected_services

    def test_observed_but_healthy_upstream_is_not_affected(self):
        """观测到、但指标正常的上游不算受影响 —— 这两者必须分开。

        这是本条规则的全部意义：把「查过，正常」与「没查」区分开，
        而不是用"有没有数据"一刀切。
        """
        result = _run(order_has_evidence=True, order_is_anomalous=False)
        assert "order-service" not in result.report.affected_services
