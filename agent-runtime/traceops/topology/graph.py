"""服务拓扑（P0 用配置文件声明，蓝图 §2.7 明确不做自动发现）。

两个方向必须分开，否则语义会错：

- `dependencies(s)`：s 调用谁 → 用于**调查范围扩展**（根因可能在我的下游）
- `dependents(s)`：谁调用 s → 用于**影响范围（blast radius）**（我挂了谁受害）

这个区分正是 F-005 的关键：症状（上游）不该被自动当成调查对象，
但它必须出现在影响范围里。
"""
from __future__ import annotations

from collections import defaultdict


class TopologyError(ValueError):
    """拓扑非法：自环、环、或查询未注册服务（strict 模式）。"""


class ServiceTopology:
    def __init__(self, edges: dict[str, set[str]]) -> None:
        deps: dict[str, set[str]] = {}
        # 先补齐所有节点，再建反图：边表在遍历中不得增删键
        for node, targets in edges.items():
            deps.setdefault(node, set()).update(targets)
            for target in targets:
                deps.setdefault(target, set())
        self._deps = deps
        self._dependents: dict[str, set[str]] = defaultdict(set)
        for source, targets in deps.items():
            for target in targets:
                self._dependents[target].add(source)

    @classmethod
    def from_edges(cls, edges: list[tuple[str, str]]) -> ServiceTopology:
        dep_map: dict[str, set[str]] = defaultdict(set)
        normalized: list[tuple[str, str]] = []
        for source, target in edges:
            if source == target:
                raise TopologyError(f"self edge is not allowed: {source!r} -> {target!r}")
            if (source, target) in normalized:
                continue  # 重复边去重
            normalized.append((source, target))
            dep_map[source].add(target)

        topology = cls({source: targets for source, targets in dep_map.items()})
        topology._reject_cycles(normalized)
        return topology

    def _reject_cycles(self, edges: list[tuple[str, str]]) -> None:
        """DFS 三色标记。环会让 blame_order 无法定义先后。"""
        graph: dict[str, list[str]] = defaultdict(list)
        for source, target in edges:
            graph[source].append(target)

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = defaultdict(int)

        def visit(node: str) -> None:
            color[node] = GRAY
            for neighbour in graph.get(node, []):
                if color[neighbour] == GRAY:
                    raise TopologyError(f"cycle detected: {node!r} -> {neighbour!r}")
                if color[neighbour] == WHITE:
                    visit(neighbour)
            color[node] = BLACK

        for node in list(graph):
            if color[node] == WHITE:
                visit(node)

    # ---- direction queries ----
    def dependencies(self, service: str) -> set[str]:
        return set(self._deps.get(service, set()))

    def dependents(self, service: str) -> set[str]:
        return set(self._dependents.get(service, set()))

    def services(self) -> set[str]:
        return set(self._deps) | set(self._dependents)

    # ---- scope ----
    def expand_scope(self, service: str, *, depth: int = 1, strict: bool = False) -> set[str]:
        """向下游（被依赖方）展开 depth 层。depth=0 只含自身。"""
        if strict and service not in self.services():
            raise TopologyError(f"service {service!r} is not registered in topology")
        scope = {service}
        frontier = {service}
        for _ in range(max(0, depth)):
            frontier = {dep for node in frontier for dep in self.dependencies(node)} - scope
            if not frontier:
                break
            scope |= frontier
        return scope

    def blast_radius(self, service: str, *, depth: int = 1) -> set[str]:
        """向上游（调用方）展开 depth 层：我挂了，谁受影响。"""
        radius = {service}
        frontier = {service}
        for _ in range(max(0, depth)):
            frontier = {dep for node in frontier for dep in self.dependents(node)} - radius
            if not frontier:
                break
            radius |= frontier
        return radius

    def sibling_scope(self, service: str, *, depth: int = 2) -> set[str]:
        """找出与 `service` **共享依赖**的兄弟服务（排除自身与依赖/调用方）。

        为什么需要：`expand_scope` 只沿下游走，于是

            api-gateway → order-service → redis
            inventory-service → redis

        从 order-service 出发永远看不到 inventory-service —— 它是**兄弟**，
        不是下游也不是上游。但根因传导对兄弟和子女是同一回事：
        redis 出问题，inventory-service 一样遭殃。

        漏掉兄弟的代价（由 F-006 场景实测）：
          1. 传播链缺节点（chain 指标失败）
          2. 影响范围漏报（affected 指标失败）
          3. **证据丢失**：兄弟的日志往往是根因机制最直接的证据
             （"redis timeout ... could not reserve stock" 就在兄弟那边），
             不查它就只能看到根因服务自己的弱信号，机制判定被带偏

        实现：取 `service` 的下游集合 D，再从 D 的**调用方**中排除自身与
        `service` 自身的下游/上游链，剩下的就是兄弟。深度 >1 时沿下游逐层扩展。
        """
        downstream = self.expand_scope(service, depth=depth)
        # 与 service 直接/间接共享任一依赖的服务
        candidates: set[str] = set()
        for node in downstream:
            candidates |= self.dependents(node)

        # 排除：自身、自身依赖链（它们属于下游）、自身调用方链（它们属于影响层）
        return candidates - downstream - self.blast_radius(service, depth=depth)

    def blame_order(self, candidates: set[str]) -> list[str]:
        """把候选按"更可能是根因"排序并保持确定性。

        判据：沿依赖链离入口越远，越可能是根因。
        api-gateway → order-service → redis 中，redis 是别人最终依赖的那一端，
        它挂会让整条链出错；api-gateway 的错误通常只是症状。

        因此用"上游层数"（有多少层调用方压在上面）作深度，深度大的排前面；
        同深度按名字升序，保证同一输入永远得到同一顺序。
        """
        depth_cache: dict[str, int] = {}

        def upstream_depth(service: str, stack: frozenset[str] = frozenset()) -> int:
            if service in depth_cache:
                return depth_cache[service]
            if service in stack:
                return 0  # 环防御（构造期已拒绝）
            callers = self.dependents(service)
            if not callers:
                depth_cache[service] = 0
                return 0
            value = 1 + max(upstream_depth(caller, stack | {service}) for caller in callers)
            depth_cache[service] = value
            return value

        return sorted(candidates, key=lambda name: (-upstream_depth(name), name))
