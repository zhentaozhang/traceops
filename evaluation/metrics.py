"""评估指标（REVISIONS-1.1 R8 口径）。

四条互不混算的口径：
1. 服务定位  : AC@1 / AC@3 / AC@5（对齐 RCAEval 的 AC@k）
2. 故障机制  : fault_type_at_1、component_at_1
3. 证据质量  : evidence_recall（ground truth 要求的证据类型是否都被引用）、
               citation_precision（被引用的证据是否都属于本场景/合法事件）
4. 弃权与结论: abstained、diagnosis_correct（INCONCLUSIVE 是合法交付物，不是失败）
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Iterable

from traceops.domain.models import EvidenceType

if TYPE_CHECKING:  # pragma: no cover
    from evaluation.scenario import Scenario
    from traceops.orchestration.graph import InvestigationResult


@dataclass(frozen=True)
class ScenarioOutcome:
    scenario_id: str
    service_ac_at_1: bool
    service_ac_at_3: bool
    service_ac_at_5: bool
    fault_type_at_1: bool
    component_at_1: bool | None
    diagnosis_correct: bool
    abstained: bool
    evidence_recall: float
    citation_precision: float
    cited_evidence: int
    fault_injected: bool
    tool_calls: int
    tokens_used: int
    chain_declared: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AggregateMetrics:
    scenario_count: int
    ac_at_1: float
    ac_at_3: float
    ac_at_5: float
    fault_type_accuracy: float
    component_accuracy: float
    diagnosis_accuracy: float
    abstention_rate: float
    evidence_recall_mean: float
    citation_precision_mean: float
    mean_tool_calls: float
    mean_tokens: float

    def to_dict(self) -> dict:
        return asdict(self)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _rank_of_service(hypotheses: list, service: str) -> int | None:
    """返回命中候选服务的最高排名（rank 1 最好）。"""
    wanted = service.strip().lower()
    ranks = [
        h.rank
        for h in hypotheses
        if h.rank is not None and (h.candidate_service or "").strip().lower() == wanted
    ]
    return min(ranks) if ranks else None


def score_scenario(scenario: "Scenario", result: "InvestigationResult") -> ScenarioOutcome:
    report = result.report
    hypotheses = list(report.hypotheses) if report is not None else []
    ground_truth = scenario.ground_truth
    expected_types = scenario.signals.expected_evidence_types()

    rank = _rank_of_service(hypotheses, ground_truth.service)
    top = hypotheses[0] if hypotheses else None

    fault_type_at_1 = bool(top is not None and top.fault_type == ground_truth.fault_type)

    if ground_truth.component is None:
        component_at_1: bool | None = None
    else:
        component_at_1 = bool(
            top is not None
            and (top.candidate_component or "").strip().lower() == ground_truth.component.strip().lower()
        )

    diagnosis_correct = bool(report is not None and report.diagnosis_status == ground_truth.expected_diagnosis)
    abstained = True if report is None else bool(report.abstained)

    cited = list(report.evidence) if report is not None else []
    present_types = {e.evidence_type for e in cited}
    evidence_recall = (
        len(expected_types & present_types) / len(expected_types) if expected_types else 0.0
    )

    wanted_service = ground_truth.service.strip().lower()
    attributable = [
        e
        for e in cited
        if e.evidence_type in expected_types
        and (e.evidence_type is EvidenceType.EVENT or (e.service_id or "").strip().lower() == wanted_service)
    ]
    citation_precision = len(attributable) / len(cited) if cited else 0.0

    return ScenarioOutcome(
        scenario_id=scenario.scenario_id,
        service_ac_at_1=rank is not None and rank <= 1,
        service_ac_at_3=rank is not None and rank <= 3,
        service_ac_at_5=rank is not None and rank <= 5,
        fault_type_at_1=fault_type_at_1,
        component_at_1=component_at_1,
        diagnosis_correct=diagnosis_correct,
        abstained=abstained,
        evidence_recall=round(evidence_recall, 4),
        citation_precision=round(citation_precision, 4),
        cited_evidence=len(cited),
        fault_injected=ground_truth.fault_type != "none",
        tool_calls=result.tool_calls,
        tokens_used=result.tokens_used,
        chain_declared=bool(ground_truth.propagation_chain),
    )


def aggregate(rows: Iterable[ScenarioOutcome]) -> AggregateMetrics:
    items = list(rows)
    if not items:
        return AggregateMetrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    component_rows = [r.component_at_1 for r in items if r.component_at_1 is not None]
    # 定位与证据类指标只在"确实注入了故障"的场景上统计：
    # 健康对照组没有可定位的根因，把它算进 AC 分母会系统性低估系统能力。
    fault_rows = [r for r in items if r.fault_injected]

    return AggregateMetrics(
        scenario_count=len(items),
        ac_at_1=_mean([1.0 if r.service_ac_at_1 else 0.0 for r in fault_rows]),
        ac_at_3=_mean([1.0 if r.service_ac_at_3 else 0.0 for r in fault_rows]),
        ac_at_5=_mean([1.0 if r.service_ac_at_5 else 0.0 for r in fault_rows]),
        fault_type_accuracy=_mean([1.0 if r.fault_type_at_1 else 0.0 for r in fault_rows]),
        component_accuracy=_mean([1.0 if v else 0.0 for v in component_rows]),
        diagnosis_accuracy=_mean([1.0 if r.diagnosis_correct else 0.0 for r in items]),
        abstention_rate=_mean([1.0 if r.abstained else 0.0 for r in items]),
        evidence_recall_mean=_mean([r.evidence_recall for r in fault_rows]),
        citation_precision_mean=_mean([r.citation_precision for r in fault_rows]),
        mean_tool_calls=_mean([float(r.tool_calls) for r in items]),
        mean_tokens=_mean([float(r.tokens_used) for r in items]),
    )
