"""拓扑采集的兄弟节点盲区：共享依赖的"旁支"服务会被整片漏掉。

## 问题（由 F-006 场景暴露）

拓扑：

```
api-gateway → order-service → redis
inventory-service → redis        ← 与 order-service 是兄弟
```

`expand_scope('order-service', depth=2)` 返回 `{order-service, redis}` ——
**只有下游链，没有兄弟**。于是 `inventory-service` 完全不被调查。

后果不是"少查一个服务"，而是：

1. **传播链缺节点**：inventory-service 明明受了影响却不在链上（F-006 `chain=0`）
2. **影响范围漏报**：报告只说 order-service 受影响（F-006 `aff=0`）
3. **机制判定被带偏**：根因 redis 的日志为空（真实原因藏在兄弟服务的日志里），
   系统只能看到 redis 错误率 0.07 这种弱信号 → 判成 availability_degradation
   而不是 pool_exhaustion（F-006 `FT@1=0`）

第 3 条最值得注意：**证据在兄弟节点那边**。inventory-service 的日志明确写着
"redis timeout ... could not reserve stock"，那正是 pool_exhaustion 的证据，
但因为我们没查它，这份证据从未存在。

## 正确行为

沿共享依赖的兄弟节点也要纳入影响层采集（不是调查层）：

- **investigation tier**：仍然只向下游展开 —— 兄弟不是候选根因
- **impact tier**：把"与受调查服务共享依赖"的兄弟也纳入，因为它们的异常
  同样是根因传导出来的

这样既补上链条/影响范围，又不会把兄弟误判成候选根因。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.topology.graph import ServiceTopology


TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)

TOPOLOGY = [
    ("api-gateway", "order-service"),
    ("order-service", "redis"),
    ("inventory-service", "redis"),
]


class TestSiblingDiscovery:
    def test_siblings_share_a_dependency(self):
        """基础事实：两个服务依赖同一个下游 = 兄弟。"""
        topology = ServiceTopology.from_edges(TOPOLOGY)
        assert "inventory-service" in topology.dependents("redis")
        assert "order-service" in topology.dependents("redis")

    def test_expand_scope_alone_misses_siblings(self):
        """记录当前行为：单独用 expand_scope 会漏掉兄弟（这是要补的口子）。"""
        topology = ServiceTopology.from_edges(TOPOLOGY)
        assert "inventory-service" not in topology.expand_scope("order-service", depth=2)

    def test_sibling_scope_finds_them(self):
        """新增能力：沿共享依赖找出兄弟服务。"""
        topology = ServiceTopology.from_edges(TOPOLOGY)
        siblings = topology.sibling_scope("order-service", depth=2)
        assert "inventory-service" in siblings, (
            f"expected siblings via redis, got {siblings}")

    def test_sibling_scope_excludes_self(self):
        topology = ServiceTopology.from_edges(TOPOLOGY)
        assert "order-service" not in topology.sibling_scope("order-service", depth=2)

    def test_sibling_scope_excludes_direct_dependencies(self):
        """redis 是依赖，不是兄弟 —— 它已在 investigation tier。"""
        topology = ServiceTopology.from_edges(TOPOLOGY)
        assert "redis" not in topology.sibling_scope("order-service", depth=2)

    def test_no_siblings_when_topology_is_a_chain(self):
        topology = ServiceTopology.from_edges([("a", "b"), ("b", "c")])
        assert topology.sibling_scope("b", depth=2) == set()

    def test_isolated_service_has_no_siblings(self):
        topology = ServiceTopology.from_edges([("a", "b")])
        assert topology.sibling_scope("a", depth=2) == set()
