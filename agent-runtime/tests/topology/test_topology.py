"""拓扑感知候选扩展测试（解锁 F-005 传播链验证）。

问题：P0 只查 service_hint 一个服务，候选服务 = 证据所属服务，因此
"上游症状"永远不会成为候选，也就无法区分根因与下游症状。

方案：ServiceTopology 声明依赖边；采集阶段按"候选服务 + 其依赖"扩展查询范围，
让上游/下游服务的证据也进 Store，这样：
- 根因服务有原始异常证据
- 症状服务只有延迟/错误证据，且其异常在时间上晚于根因
"""
from __future__ import annotations

import pytest

from traceops.topology.graph import ServiceTopology, TopologyError


def _topology() -> ServiceTopology:
    return ServiceTopology.from_edges([
        ("order-service", "redis"),
        ("order-service", "mysql"),
        ("api-gateway", "order-service"),
        ("payment-service", "billing-api"),
    ])


class TestTopologyConstruction:
    def test_from_edges_builds_both_directions(self):
        topo = _topology()
        assert topo.dependencies("order-service") == {"redis", "mysql"}
        assert topo.dependents("order-service") == {"api-gateway"}
        assert topo.dependencies("api-gateway") == {"order-service"}
        assert topo.dependents("redis") == {"order-service"}

    def test_unknown_service_has_empty_sets(self):
        topo = _topology()
        assert topo.dependencies("unknown") == set()
        assert topo.dependents("unknown") == set()

    def test_self_edge_rejected(self):
        with pytest.raises(TopologyError, match="self"):
            ServiceTopology.from_edges([("a", "a")])

    def test_cycle_detected(self):
        with pytest.raises(TopologyError, match="cycle"):
            ServiceTopology.from_edges([("a", "b"), ("b", "c"), ("c", "a")])

    def test_duplicate_edge_is_deduplicated(self):
        topo = ServiceTopology.from_edges([("a", "b"), ("a", "b")])
        assert topo.dependencies("a") == {"b"}


class TestCandidateExpansion:
    def test_expand_includes_self_and_dependencies(self):
        topo = _topology()
        scope = topo.expand_scope("order-service", depth=1)
        assert scope == {"order-service", "redis", "mysql"}

    def test_expand_depth_two_reaches_transitive(self):
        topo = _topology()
        scope = topo.expand_scope("api-gateway", depth=2)
        assert scope == {"api-gateway", "order-service", "redis", "mysql"}

    def test_expand_never_includes_dependents(self):
        """只向下游展开：症状（上游调用方）不该被自动当成调查对象。"""
        topo = _topology()
        scope = topo.expand_scope("order-service", depth=2)
        assert "api-gateway" not in scope

    def test_depth_zero_is_self_only(self):
        assert _topology().expand_scope("order-service", depth=0) == {"order-service"}

    def test_blast_radius_includes_dependents(self):
        """影响范围是反方向：谁会被这个服务的故障波及。"""
        topo = _topology()
        assert topo.blast_radius("redis", depth=1) == {"redis", "order-service"}

    def test_blame_ordering_root_before_symptom(self):
        """沿依赖链分层的顺序：越靠下游（依赖别人）的越可能是根因。"""
        topo = _topology()
        order = topo.blame_order({"api-gateway", "order-service", "redis"})
        assert order.index("redis") < order.index("order-service") < order.index("api-gateway")

    def test_disconnected_services_are_ordered_deterministically(self):
        topo = _topology()
        a = topo.blame_order({"redis", "billing-api"})
        b = topo.blame_order({"billing-api", "redis"})
        assert a == b

    def test_scope_excludes_unknown_when_strict(self):
        with pytest.raises(TopologyError, match="not registered"):
            _topology().expand_scope("ghost-service", depth=1, strict=True)
