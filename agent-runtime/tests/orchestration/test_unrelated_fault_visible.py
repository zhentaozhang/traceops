"""无关链路上的第二故障：从单一告警起点出发必须能被发现。

## 问题的结构

调查起点来自**一条告警**的 `service_hint`。`_scope_services` 从该点出发：

- investigation tier：向下游展开
- impact tier：向上游 + 兄弟节点

但 `billing-api` 与 `order-service` 的关系是：

```
api-gateway → order-service → redis
api-gateway → payment-service → billing-api
```

两条链只在 **api-gateway**（共同的调用方）汇合。从 order-service 出发：
- 下游 = {redis}
- 上游 = {api-gateway}
- 兄弟 = 共享依赖的服务 = {}（order-service 只依赖 redis）

于是 billing-api 整个不可见。这与 aurora-lab 场景 12 是同一种失败：
**报告只覆盖它看得到的那条链，第二条故障永远不出现，而报告不会说"我没查"**。

## 正确行为（不是扩大采集，而是承认边界）

从单一告警起点，系统**不可能知道**另一条链上有故障。所以正确做法不是
"想办法查遍全世界"，而是：

1. 在 `missing_data` 里明确声明"只调查了 X 这条链，其他链路未覆盖"
2. 不得把未调查的链路暗示为健康

第 1 条是可实现且诚实的；第 2 条是我们已经在做的（影响范围只报有证据的）。

这是多根因能力的**边界声明**，不是能力本身 —— 能力需要多告警接入
（一个 incident 关联多条告警时才能覆盖多条链）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest, RunContext, run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)

# 两条独立链，只在 api-gateway 汇合
TWO_CHAINS = [
    ("api-gateway", "order-service"),
    ("order-service", "redis"),
    ("api-gateway", "payment-service"),
    ("payment-service", "billing-api"),
]


class HealthOnlyAdapters:
    """数据源正常，但什么都查不到 —— 逼出"边界声明"。"""

    def query_service_error_rate(self, *, service, start, end):
        raise ValueError(f"no data for {service}")

    def query_service_logs(self, *, service, start, end, filter=None):
        raise ValueError(f"no data for {service}")

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        raise ValueError(f"no data for {service}")


def _run(adapters, topology_edges=TWO_CHAINS):
    topology = ServiceTopology.from_edges(topology_edges) if topology_edges else None
    ctx = RunContext(store=EvidenceStore(), adapters=adapters,
                     budget=BudgetManager(max_tool_calls=40), topology=topology,
                     scope_depth=2)
    return run_investigation(
        InvestigationRequest(
            investigation_run_id="run", incident_id="inc", environment="demo",
            service_hint="order-service", fault_start=TS,
            window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        ), ctx)


class TestUnobservedChainsAreDeclared:
    def test_report_says_which_chain_was_covered(self):
        """报告必须说明调查覆盖了哪条链 —— 沉默等于暗示"全都查过了"。"""
        result = _run(HealthOnlyAdapters())
        text = " ".join(result.report.missing_data).lower()
        assert "order-service" in text or "chain" in text or "scope" in text

    def test_unrelated_chain_is_not_claimed_healthy(self):
        """未调查的体系不得出现在影响范围（那是发明事实）。"""
        result = _run(HealthOnlyAdapters())
        assert "billing-api" not in result.report.affected_services
        assert "payment-service" not in result.report.affected_services


class TestScopedServicesAreTheOnesActuallyQueried:
    def test_only_the_alarms_chain_is_in_scope(self):
        """记录边界：从 order-service 出发看不到 billing-api 那条链。"""
        topology = ServiceTopology.from_edges(TWO_CHAINS)
        visible = topology.expand_scope("order-service", depth=2)
        assert "billing-api" not in visible
        assert "redis" in visible


class TestExplicitScopeDeclaration:
    """需要一个**主动的**边界声明，而不是碰巧 missing_data 里有东西。"""

    def test_declares_services_outside_the_investigated_chain(self):
        """报告应显式列出"未被调查"的服务，让读者知道覆盖边界。"""
        result = _run(HealthOnlyAdapters())
        text = " ".join(result.report.missing_data)
        # 拓扑里存在但与本次调查无关的服务应当被点名
        assert "not investigated" in text.lower() or "outside" in text.lower(), (
            f"expected an explicit scope-boundary note, got: {result.report.missing_data}")

    def test_declaration_lists_the_unreached_services(self):
        topology = ServiceTopology.from_edges(TWO_CHAINS)
        investigated = topology.expand_scope("order-service", depth=2) | \
            topology.blast_radius("order-service", depth=2) | \
            topology.sibling_scope("order-service", depth=2)
        unreached = topology.services() - investigated
        assert "billing-api" in unreached
        assert "payment-service" in unreached
