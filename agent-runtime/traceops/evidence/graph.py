"""证据关联与证据图（蓝图 §2.3「证据中心」/ §6.3 Correlator / §6.4 Graph Builder）。

P0 范围（REVISIONS-1.1 R1）：不做独立 Graph Engine，但要提供**基础关联逻辑**：

- 时间关联：异常是否落在同一故障窗口、谁先谁后
- 服务关联：按拓扑判断是否同一调用链的上下游
- Trace 关联：按 trace_id 把日志和链路挂到一起
- 事件关联：变更事件进时间线

关键区分（蓝图 §6.4）：Service Dependency Graph（架构图）与 Evidence Graph（事实图）
是两张图，语义完全不同，这里只构建后者。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from traceops.domain.models import (
    Evidence,
    EvidenceRelation,
    EvidenceRelationType,
    EvidenceType,
)

DEFAULT_CORRELATION_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True)
class CorrelationRule:
    name: str
    description: str


# 四种关联（蓝图 §6.3），顺序即优先级
TEMPORAL = CorrelationRule("TEMPORAL", "两条证据的观测时间在关联窗口内")
SAME_TRACE = CorrelationRule("SAME_TRACE", "两条证据共享同一个 trace_id")
SERVICE_LINK = CorrelationRule("SERVICE_LINK", "两个服务的拓扑关系可解释证据的关联")
CAUSAL_PRECEDES = CorrelationRule("CAUSAL_PRECEDES", "同一关联链内，A 明确早于 B（时间先后，不是因果证明）")


@dataclass
class EvidenceEdge:
    source_id: str
    target_id: str
    rule: str
    explanation: str
    # 时间先后不等于因果，这里只记录顺序，由诊断层负责解释
    time_delta_seconds: float | None = None


@dataclass
class EvidenceGraph:
    evidence: dict[str, Evidence] = field(default_factory=dict)
    edges: list[EvidenceEdge] = field(default_factory=list)

    def add(self, evidence: Evidence) -> None:
        self.evidence[evidence.id] = evidence

    def neighbours(self, evidence_id: str) -> list[EvidenceEdge]:
        return [e for e in self.edges if evidence_id in (e.source_id, e.target_id)]

    def timeline(self) -> list[tuple[str, str]]:
        """按 observed_at 排序的 (time_iso, evidence_id) 列表。"""
        ordered = sorted(self.evidence.values(), key=lambda e: (e.observed_at, e.id))
        return [(e.observed_at.isoformat(), e.id) for e in ordered]

    def services(self) -> set[str]:
        return {e.service_id for e in self.evidence.values() if e.service_id}

    def of_type(self, evidence_type: EvidenceType) -> list[Evidence]:
        return [e for e in self.evidence.values() if e.evidence_type is evidence_type]


def build_evidence_graph(
    evidence_list: list[Evidence],
    *,
    topology=None,
    window: timedelta = DEFAULT_CORRELATION_WINDOW,
) -> EvidenceGraph:
    """构建证据图。确定性：同样的输入永远得到同样的边集合与顺序。"""
    graph = EvidenceGraph()
    for evidence in evidence_list:
        graph.add(evidence)

    ordered = sorted(evidence_list, key=lambda e: (e.observed_at, e.id))

    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            delta = (second.observed_at - first.observed_at).total_seconds()
            if delta > window.total_seconds():
                continue  # 超出关联窗口，不再看作同一次故障链

            rules_applied: list[tuple[CorrelationRule, str]] = []

            if first.trace_id and first.trace_id == second.trace_id:
                rules_applied.append((SAME_TRACE, f"shared trace_id {first.trace_id}"))
            else:
                rules_applied.append((TEMPORAL, f"observed {delta:.0f}s apart"))

            if (
                first.service_id and second.service_id
                and first.service_id != second.service_id
                and topology is not None
                and (
                    second.service_id in topology.dependencies(first.service_id)
                    or first.service_id in topology.dependencies(second.service_id)
                )
            ):
                rules_applied.append(
                    (SERVICE_LINK, f"{first.service_id} <-> {second.service_id} in topology"))

            if first.evidence_type is EvidenceType.EVENT or second.evidence_type is EvidenceType.EVENT:
                rules_applied.append((CAUSAL_PRECEDES, "change event anchors the timeline"))

            for rule, explanation in rules_applied:
                graph.edges.append(EvidenceEdge(
                    source_id=first.id, target_id=second.id,
                    rule=rule.name, explanation=explanation,
                    time_delta_seconds=delta,
                ))
    return graph


def earliest_anomaly(evidence_list: list[Evidence], *, evidence_types: set[EvidenceType] | None = None) -> Evidence | None:
    """最早出现的观测类证据（排除变更事件）。

    用途：判断"谁最先坏"，是区分根因与症状的时间依据之一。
    """
    types = evidence_types or {EvidenceType.METRIC, EvidenceType.LOG, EvidenceType.TRACE}
    candidates = [e for e in evidence_list if e.evidence_type in types]
    if not candidates:
        return None
    return min(candidates, key=lambda e: (e.observed_at, e.id))


def ordering_violations(evidence_list: list[Evidence], *, expected_order: list[str]) -> list[str]:
    """按服务期望的先后顺序，检查实际观测顺序是否一致。

    用于 F-005 类场景：如果症状服务先于根因服务出现异常，说明时间线被污染
    （时钟漂移、或 ground truth 标错）。返回违反项描述，空列表表示一致。
    """
    first_seen: dict[str, Evidence] = {}
    for evidence in sorted(evidence_list, key=lambda e: (e.observed_at, e.id)):
        service = evidence.service_id
        if service and service not in first_seen:
            first_seen[service] = evidence

    violations: list[str] = []
    for before, after in zip(expected_order, expected_order[1:]):
        if before not in first_seen or after not in first_seen:
            continue
        if first_seen[before].observed_at > first_seen[after].observed_at:
            violations.append(
                f"{after} observed before {before} "
                f"({first_seen[after].observed_at.isoformat()} < {first_seen[before].observed_at.isoformat()})"
            )
    return violations


# ---------------- 内部边 → 领域关系 ----------------

# 内部规则名 → 领域关系类型的映射。
# 不是一一对应：内部 SERVICE_LINK 表达"两个服务有拓扑关系"，
# 对外最合适的表达是 AFFECTS（这条证据影响了那个服务）。
_RULE_TO_RELATION = {
    SAME_TRACE.name: EvidenceRelationType.SAME_TRACE,
    TEMPORAL.name: EvidenceRelationType.PRECEDES,
    SERVICE_LINK.name: EvidenceRelationType.AFFECTS,
    CAUSAL_PRECEDES.name: EvidenceRelationType.PRECEDES,
}


def promote_relations(graph: EvidenceGraph) -> list[EvidenceRelation]:
    """把内部 EvidenceEdge 提升为可持久化的领域 EvidenceRelation。

    为什么需要这一步：`EvidenceEdge` 是"图构建过程"的中间产物，
    住在内存里；`EvidenceRelation` 才是可以写库、可以查询、可以被审计的
    领域对象。不提升，关系就随进程一起消失。

    只提升映射表里有的规则：未知规则宁可不持久化，也不要猜一个语义出去。
    """
    relations: list[EvidenceRelation] = []
    for edge in graph.edges:
        relation_type = _RULE_TO_RELATION.get(edge.rule)
        if relation_type is None:
            continue

        source = graph.evidence.get(edge.source_id)
        target = graph.evidence.get(edge.target_id)
        if source is None or target is None:
            continue
        if source.id == target.id:
            continue  # 模型层禁止自环，这里同样不构造

        kwargs: dict = {
            "source_evidence_id": source.id,
            "target_evidence_id": target.id,
            "relation_type": relation_type,
            "explanation": edge.explanation,
        }
        if relation_type is EvidenceRelationType.SAME_TRACE:
            kwargs["trace_id"] = source.trace_id
        elif relation_type is EvidenceRelationType.PRECEDES:
            kwargs["time_delta_seconds"] = max(0.0, edge.time_delta_seconds or 0.0)
        elif relation_type is EvidenceRelationType.AFFECTS:
            kwargs["affected_service"] = target.service_id or source.service_id

        try:
            relations.append(EvidenceRelation(**kwargs))
        except ValueError:
            # 单条关系不满足模型约束（如 AFFECTS 缺服务名）时跳过它，
            # 而不是让整张图的关系都不可用。
            continue
    return relations
