"""传播链构建（修复 chain 指标的正解）。

## 为什么需要独立于 hypotheses

候选假设回答的是「哪个服务/组件坏了」——一个服务只应在**有独立故障机制**时
才成为候选。api-gateway 在级联故障里没有自己的故障机制，它的错误是 redis 传导过来的。
把它硬塞进 hypotheses，等于让系统编造一个不存在的故障。

但影响分析又确实需要表达「故障怎么传过去的」。所以拆成两个概念：

- `RootCauseHypothesis` 集合 → 候选根因（带 fault_type / failure_mechanism）
- `PropagationChain`     → 传播路径（带 role: root / affected，每跳挂证据）

## 铁律：链上每一跳必须有证据

拓扑告诉你"这两个服务有关系"，但不告诉你"这次故障真的沿这条边走"。
没有证据就进链 = 拿拓扑编故事。所以本模块只收录**有证据的服务**。

## 顺序按拓扑距离，不按证据时间

证据到达时间受采集顺序影响，噪声大。拓扑距离是结构性的、稳定的。
时间顺序只用于**检测异常**（症状早于根因 → 时间线可疑），不用于排序。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from traceops.domain.models import Evidence


@dataclass
class PropagationNode:
    """传播链上的一跳。"""

    service: str
    role: str  # "root" | "affected"
    position: int  # 从 1 开始，1 = 根因
    evidence_ids: list[str] = field(default_factory=list)
    first_observed_at: datetime | None = None
    # None = 根因（没有前驱）；False = 比前驱更早（时间线可疑）
    observed_after_predecessor: bool | None = None

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "role": self.role,
            "position": self.position,
            "evidence_ids": list(self.evidence_ids),
            "first_observed_at": self.first_observed_at.isoformat() if self.first_observed_at else None,
            "observed_after_predecessor": self.observed_after_predecessor,
        }


def build_propagation_chain(
    *,
    root_service: str,
    evidence_list: list[Evidence],
    topology,
    max_depth: int = 2,
) -> list[PropagationNode]:
    """构建传播链。

    Args:
        root_service: 首选根因服务（来自排序第一的假设）
        evidence_list: 本次调查收集到的全部证据
        topology: ServiceTopology；None 时退化为「只有根因」
        max_depth: 向下游/上游展开的层数上限

    Returns:
        按 position 升序的链；空列表表示根因本身都没有证据。
    """
    if topology is None:
        candidates = {root_service}
    else:
        # 受影响节点既可能在下游（被根因拖累）也可能在上游（调用了根因），两边都要看
        downstream = topology.expand_scope(root_service, depth=max_depth)
        upstream = topology.blast_radius(root_service, depth=max_depth)
        candidates = downstream | upstream

    # 只保留有证据的服务；证据按服务聚合
    evidence_by_service: dict[str, list[Evidence]] = {}
    for evidence in evidence_list:
        if evidence.service_id is None:
            continue  # 变更事件没有归属服务，不进链
        if evidence.service_id not in candidates:
            continue
        evidence_by_service.setdefault(evidence.service_id, []).append(evidence)

    if root_service not in evidence_by_service:
        return []  # 根因自己都没有证据 → 不构造链（不能凭空断言根因）

    # 按拓扑距离排序：根因在前，越远越后
    ordered = _order_by_distance(root_service, list(evidence_by_service), topology)

    chain: list[PropagationNode] = []
    previous_observed_at: datetime | None = None
    for position, service in enumerate(ordered, start=1):
        group = sorted(evidence_by_service[service], key=lambda e: (e.observed_at, e.id))
        first_observed = group[0].observed_at
        if position == 1:
            after_predecessor: bool | None = None
        else:
            after_predecessor = (
                previous_observed_at is not None and first_observed >= previous_observed_at
            )
        chain.append(
            PropagationNode(
                service=service,
                role="root" if position == 1 else "affected",
                position=position,
                evidence_ids=[e.id for e in group],
                first_observed_at=first_observed,
                observed_after_predecessor=after_predecessor,
            )
        )
        previous_observed_at = first_observed

    return chain


def _order_by_distance(root_service: str, services: list[str], topology) -> list[str]:
    """按到根因的拓扑距离排序；同距离按名称升序（保证确定性）。"""
    if topology is None:
        return [root_service]

    distances: dict[str, int] = {root_service: 0}
    frontier = {root_service}
    depth = 0
    # 双向 BFS：下游（依赖）与上游（调用方）都算距离
    while frontier:
        depth += 1
        next_frontier: set[str] = set()
        for service in frontier:
            neighbours = topology.dependencies(service) | topology.dependents(service)
            for neighbour in neighbours:
                if neighbour in distances or neighbour not in services:
                    continue
                distances[neighbour] = depth
                next_frontier.add(neighbour)
        frontier = next_frontier

    # 拓扑上够不到的服务（理论上不会发生，因为 candidates 来自拓扑）放到最后
    unreachable = [s for s in services if s not in distances]
    reachable = sorted(
        (s for s in services if s in distances),
        key=lambda s: (distances[s], s),
    )
    return reachable + sorted(unreachable)
