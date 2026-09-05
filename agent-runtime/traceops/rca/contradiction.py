"""确定性矛盾检测：把「反向证据」从"靠运气收到"变成"主动构造"。

## 现状的问题

`RelationType.CONTRADICTS` 在领域模型里存在，`ranking._evaluate` 也读
`contradicting_evidence_count`（R-005），但**没有任何地方会生成 CONTRADICTS 关系**。
于是：

- `评测口径` 明说「P0 没有 CONTRADICTS 关系的生成器，分母恒为 0」
- `Contradiction Detection Rate` 指标永远算不出来
- 一个"假设与观测直接冲突"的场景，系统会照样给 SUPPORTED

这不是"缺一个 P1 功能"，而是**排序规则 R-005 是死代码**。

## 为什么可以完全确定性（不需要 LLM）

矛盾的判据都是可枚举的，且不依赖语义理解：

| 假设 | 反向证据 | 判据 |
|---|---|---|
| `pool_exhaustion` | 同服务指标显示错误率**正常** | 错误率序列最大值 < 阈值 |
| `latency_degradation` | 同服务 span **全部很快** | 最慢 span < 慢阈值 |
| `availability_degradation` | 同服务有**正常**的请求日志 | 日志里无 error 且无超时关键词 |
| 任意假设 | 采集到的证据**归属另一个服务**且无任何指向本服务 | 归因全在他处 |

关键：这些判据都是"我们确实观测了，且观测结果与假设相反"。
如果根本没采到数据，那是 `NO_DATA`，**不是矛盾** —— 不能把
"没看见"当成"看见反证"（aurora-lab E-2）。

## 为什么这比 LLM 更合适

aurora 的 C1 思想是「Design queries to disprove your hypothesis」。
我们受查询模板约束，不能自由设计查询，但**可以在已有证据上做反证判定**。
这既满足"主动找反证"，又保持确定性、可复现、可解释。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from traceops.domain.models import (
    Evidence, EvidenceType, RelationType, RootCauseHypothesis,
)
from traceops.orchestration.detectors import (
    ERROR_RATE_THRESHOLD, SLOW_SPAN_THRESHOLD_SECONDS, attribute_evidence,
)


@dataclass(frozen=True)
class Contradiction:
    """一条把假设推翻的证据。"""

    hypothesis_id: str
    evidence_id: str
    reason: str


def _owned_by(evidence: Evidence, service: str, topology) -> bool:
    """这条证据是否'关于'某个服务（与诊断口径同源）。"""
    attribution = attribute_evidence(evidence, topology)
    if attribution is None:
        return False
    return attribution[0] == service


def _metric_is_reassuring(evidence: Evidence) -> bool:
    """错误率指标**全部**低于阈值 —— 观测结果与'故障'相反。

    注意：空序列不算 reassuring（那是没数据，不是"正常"）。
    """
    series = evidence.normalized_data.get("series") or []
    saw_any = False
    for item in series:
        for point in item.get("values") or []:
            try:
                saw_any = True
                if float(point[1]) >= ERROR_RATE_THRESHOLD:
                    return False
            except (TypeError, ValueError, IndexError):
                continue
    return saw_any


def _logs_are_reassuring(evidence: Evidence) -> bool:
    """日志里**没有**任何错误/超时痕迹 —— 且确实有日志行。"""
    records = evidence.normalized_data.get("records") or []
    if not records:
        return False
    hints = ("error", "exception", "timeout", "timed out", "refused", "unavailable", "exhausted")
    for record in records:
        blob = f"{record.get('message', '')} {record.get('labels', {}).get('level', '')}".lower()
        if any(h in blob for h in hints):
            return False
    return True


def _traces_are_fast(evidence: Evidence) -> bool:
    """所有 span 都快于慢阈值 —— 与'延迟劣化'假设相反。"""
    spans = evidence.normalized_data.get("spans") or []
    if not spans:
        return False
    for span in spans:
        try:
            if float(span.get("duration_us") or 0) / 1_000_000 >= SLOW_SPAN_THRESHOLD_SECONDS:
                return False
        except (TypeError, ValueError):
            return False
    return True


# 哪些假设会被哪类"令人安心"的观测推翻
_FALSIFIERS = {
    "availability_degradation": {
        EvidenceType.METRIC: (_metric_is_reassuring, "error-rate stays below threshold"),
        EvidenceType.LOG: (_logs_are_reassuring, "logs contain no error signatures"),
    },
    "pool_exhaustion": {
        EvidenceType.METRIC: (_metric_is_reassuring, "error-rate stays below threshold"),
        EvidenceType.LOG: (_logs_are_reassuring, "logs contain no exhaustion signatures"),
    },
    "latency_degradation": {
        EvidenceType.TRACE: (_traces_are_fast, "all spans are below the slow threshold"),
    },
    # 依赖超时：如果错误率全程正常，那"超时"就没有在用户可见层面发生。
    # 只靠日志是不够的 —— 日志里出现 timeout 恰恰是**支持**该假设的证据。
    "dependency_timeout": {
        EvidenceType.METRIC: (_metric_is_reassuring, "error-rate stays below threshold"),
    },
}


def find_contradictions(
    hypotheses: list[RootCauseHypothesis],
    evidence_list: list[Evidence],
    *,
    topology: Any | None = None,
) -> list[Contradiction]:
    """为每个假设找出**明确推翻它**的证据。

    只收录同时满足两条的证据：
      1. 它属于该假设的候选服务（归属与诊断口径同源）
      2. 它的观测结果与假设的故障机制**相反**

    任一条件不满足就不收录 —— 宁可不报矛盾，也不要造假矛盾。
    """
    found: list[Contradiction] = []
    for hypothesis in hypotheses:
        service = (hypothesis.candidate_service or "").strip()
        if not service:
            continue
        rules = _FALSIFIERS.get((hypothesis.fault_type or "").strip())
        if not rules:
            continue
        for evidence in evidence_list:
            rule = rules.get(evidence.evidence_type)
            if rule is None:
                continue
            predicate, explanation = rule
            if not _owned_by(evidence, service, topology):
                continue
            if predicate(evidence):
                found.append(Contradiction(
                    hypothesis_id=hypothesis.id,
                    evidence_id=evidence.id,
                    reason=f"{evidence.evidence_type.value} contradicting "
                           f"{hypothesis.fault_type}: {explanation}",
                ))
    return found


def relation_type_of(_contradiction: Contradiction) -> RelationType:
    return RelationType.CONTRADICTS
