"""拓扑漂移：声明的关系与实际被调用到的关系对不上时，说出来。

## 被替换的缺陷

`topology_edges` 是请求里带进来的声明，整趟调查把它当事实用
（`blast_radius`、`blame_order`、`_unobserved_related` 都依赖它）。
但声明会过期：拆掉的服务还在拓扑文件里、新加的调用还没有人补声明。
调查不会因此报错 —— 它会**安静地按一张错的图推理**。

更糟的是"未声明"这一侧：拓扑里没有的依赖，`blast_radius` 不会覆盖到，
于是它既不进影响范围、也不进 `_unobserved_related`（那份清单是从拓扑推的），
而它可能正是根因所在 —— 一个拓扑里根本不存在的服务导致的全链路超时，
在报告里连"我没查过它"都不会出现。

## 怎么在没有 parentSpanId 的情况下推出边

OTLP 分支**不提取 `parentSpanId`**（见 `normalizer` 的注释），所以无法从
span 还原调用父子关系。但不需要：日志与链路里的文本会点名它碰了谁
（"could not get a resource from the pool … redis"），
`detectors.attribute_evidence` 已经把这件事做完了 ——
沿着它给出的归属，`(发起方, 被归属方)` 就是一条**被实际用到过**的边。

## 为什么叫"未取证"而不是"已死"

声明了但本窗口没有任何证据的边，只能说明"这次没被用到"，
不能说明"这条边断了"。把沉默当死亡是这套系统反复踩过的坑
（`_observed_services` 的注释里写着同一件事）。所以字段名与报告文案
都只说"未取证"，并且只作为提示进 `missing_data`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from traceops.domain.models import Evidence, EvidenceType
from traceops.orchestration.detectors import attribute_evidence


@dataclass(frozen=True)
class TopologyDrift:
    """声明的边 vs 本窗口真正被用到的边。"""

    declared_edges: list[tuple[str, str]]
    observed_edges: list[tuple[str, str]]
    #: 观测到、但拓扑里没有 —— 这是**真信号**：拓扑缺了这条边。
    undeclared_edges: list[tuple[str, str]]
    #: 拓扑里有、本窗口没用上 —— 只能说明"没取证"，不能说明"断了"。
    unobserved_declared_edges: list[tuple[str, str]]

    @property
    def has_drift(self) -> bool:
        return bool(self.undeclared_edges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared_edges": [list(edge) for edge in self.declared_edges],
            "observed_edges": [list(edge) for edge in self.observed_edges],
            "undeclared_edges": [list(edge) for edge in self.undeclared_edges],
            "unobserved_declared_edges": [
                list(edge) for edge in self.unobserved_declared_edges
            ],
            "has_drift": self.has_drift,
        }


def discover_edges(evidence: Iterable[Evidence]) -> set[tuple[str, str]]:
    """从日志/链路证据里推出"谁在调用谁"。

    只看 LOG 与 TRACE：指标是服务自身状态，变更是"对象"而不是"关系"，
    把它们算成边会把"redis 的指标"读成"某服务调用了 redis"。
    """
    edges: set[tuple[str, str]] = set()
    for item in evidence:
        if item.evidence_type not in (EvidenceType.LOG, EvidenceType.TRACE):
            continue
        if not item.service_id:
            continue
        # 不传 topology：要的是"文本里真的点名了谁"，
        # 而不是"按声明过滤后剩下的那个"。传了拓扑就看不见未声明的边了 ——
        # 而未声明的边恰恰是这份清单存在的理由。
        attribution = attribute_evidence(item, None)
        if attribution is None:
            continue
        owner, _component = attribution
        if owner and owner != item.service_id:
            edges.add((item.service_id, owner))
    return edges


def diff_topology(declared: Any, observed: set[tuple[str, str]]) -> TopologyDrift:
    """比较声明与观测。`declared` 可以是 `ServiceTopology` 或边列表。"""
    declared_edges = _declared_edges(declared)
    declared_set = set(declared_edges)
    return TopologyDrift(
        declared_edges=sorted(declared_edges),
        observed_edges=sorted(observed),
        undeclared_edges=sorted(observed - declared_set),
        unobserved_declared_edges=sorted(declared_set - observed),
    )


def _declared_edges(declared: Any) -> list[tuple[str, str]]:
    if declared is None:
        return []
    if hasattr(declared, "services") and hasattr(declared, "dependencies"):
        edges: list[tuple[str, str]] = []
        for caller in declared.services():
            for callee in declared.dependencies(caller):
                edges.append((caller, callee))
        return edges
    edges = []
    for pair in declared:
        caller, callee = pair[0], pair[1]
        edges.append((caller, callee))
    return edges
