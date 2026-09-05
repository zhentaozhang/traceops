"""语义校验：假设声称"应该会看到什么"，就必须真的去核对。

## 这是六类校验里唯一还缺的一类

蓝图 §6.5 列了六个验证器，前四类（Provenance / Scope / Temporal / Reference）
都已实现，第五类 Semantic Validator 一直是 ⬜。

`需求文档` 把它标成「需人工标注数据」，理由是"证据是否支持假设"看起来
需要语义理解。但**其中一半是纯确定性的**：

每个故障机制都有**可枚举的预期观测**。例如「连接池耗尽」预期看到
超时日志、错误率上升、慢 span；「延迟劣化」预期看到慢 span 但没有错误日志。
只要假设声明的预期观测中有哪些被真实证据命中/缺失，就能算出覆盖率——
不需要 LLM，也不需要人工标注。

## 为什么这个校验重要（而不是又一个自检）

现在的 `SUPPORTED` 只看 `supporting_evidence_count > 0`。
一条含 "timeout" 的日志就能让任意假设变 SUPPORTED。语义校验回答的是
**更精确的问题**：这个假设**声称**的那组观测，实际命中了几项？

aurora 的 conclusion_gate 有一句话正是这个意思：
「Absence of **expected** evidence is evidence」——
预期证据的缺失本身就是证据。这正是覆盖率能表达的东西。

## 分工

- 确定性（本模块）：预期观测的覆盖率 + 缺失清单
- 需要 LLM / 人工（仍然不做）：判断"证据的语义内容是否真的指向该机制"
"""
from __future__ import annotations

from dataclasses import dataclass, field

from traceops.domain.models import Evidence, EvidenceType, RootCauseHypothesis


@dataclass(frozen=True)
class SemanticCheck:
    """一个假设的语义校验结果。"""

    hypothesis_id: str
    expected: tuple[str, ...]
    observed: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def coverage(self) -> float:
        """命中的预期观测占全部预期的比例。没有预期时返回 0（无法校验）。"""
        if not self.expected:
            return 0.0
        return round(len(self.observed) / len(self.expected), 4)

    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "expected": list(self.expected),
            "observed": list(self.observed),
            "missing": list(self.missing),
            "coverage": self.coverage,
        }


# 每种故障机制"应该会看到什么"。键必须与 detectors 产出的一致。
EXPECTATIONS: dict[str, tuple[str, ...]] = {
    "availability_degradation": ("error_rate_rise", "error_logs"),
    "pool_exhaustion": ("error_rate_rise", "error_logs", "slow_span"),
    "latency_degradation": ("slow_span",),
    "dependency_timeout": ("error_logs", "error_rate_rise"),
}


def _observed_markers(evidence_list: list[Evidence], service: str, topology=None) -> set[str]:
    """从证据里提取「实际观测到了什么」的标记。

    这里刻意**不复用 anomaly 检测器**：检测器的输入是单条证据、输出是布尔，
    而语义校验要的是"这类观测存在不存在"。两者粒度不同，
    混用会让"某一条日志不是错误"被误读成"这一类观测不存在"。
    """
    from traceops.orchestration.detectors import attribute_evidence

    markers: set[str] = set()
    for evidence in evidence_list:
        attribution = attribute_evidence(evidence, topology)
        owner = attribution[0] if attribution else evidence.service_id
        if owner != service:
            continue

        if evidence.evidence_type is EvidenceType.METRIC:
            series = evidence.normalized_data.get("series") or []
            for item in series:
                for point in item.get("values") or []:
                    try:
                        if float(point[1]) >= 0.05:
                            markers.add("error_rate_rise")
                    except (TypeError, ValueError, IndexError):
                        continue

        elif evidence.evidence_type is EvidenceType.LOG:
            records = evidence.normalized_data.get("records") or []
            hints = ("error", "exception", "timeout", "timed out", "refused",
                     "unavailable", "exhausted")
            for record in records:
                blob = f"{record.get('message', '')} {record.get('labels', {}).get('level', '')}".lower()
                if any(hint in blob for hint in hints):
                    markers.add("error_logs")
                    break

        elif evidence.evidence_type is EvidenceType.TRACE:
            for span in evidence.normalized_data.get("spans") or []:
                try:
                    if float(span.get("duration_us") or 0) / 1_000_000 >= 0.5:
                        markers.add("slow_span")
                        break
                except (TypeError, ValueError):
                    continue
    return markers


def check_hypothesis(
    hypothesis: RootCauseHypothesis,
    evidence_list: list[Evidence],
    *,
    topology=None,
) -> SemanticCheck:
    """核对一个假设声称的预期观测是否被证据覆盖。"""
    expected = EXPECTATIONS.get((hypothesis.fault_type or "").strip(), ())
    observed = _observed_markers(evidence_list, (hypothesis.candidate_service or "").strip(), topology)
    hit = tuple(m for m in expected if m in observed)
    return SemanticCheck(
        hypothesis_id=hypothesis.id,
        expected=expected,
        observed=hit,
        missing=tuple(m for m in expected if m not in observed),
    )


def check_all(
    hypotheses: list[RootCauseHypothesis],
    evidence_list: list[Evidence],
    *,
    topology=None,
) -> list[SemanticCheck]:
    return [check_hypothesis(h, evidence_list, topology=topology) for h in hypotheses]
