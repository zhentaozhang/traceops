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

from evaluation.rank_quality import average_precision_at, mean_reciprocal_rank
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
    chain_declared: bool
    chain_order_correct: bool | None
    affected_services_correct: bool | None
    tool_calls: int
    tokens_used: int
    # rubric 结果（未声明 rubric 的场景为空元组，保持向后兼容）
    missing_identifications: tuple[str, ...] = ()
    wrong_answers_asserted: tuple[str, ...] = ()
    # 该场景是否声明过 rubric。必须显式记录：空元组无法区分
    # 「没声明 rubric」与「声明了但全部命中」，后者应当进分母。
    rubric_declared: bool = False
    # 根因服务在候选里的排名（None = 未进候选）。Avg@k/MRR 由它派生。
    ground_truth_rank: int | None = None
    # 候选池大小（去重后的候选服务数）。AC@k 必须与它一起看：
    # 池子只有 1 个候选时，AC@3/AC@5 恒真，是伪指标。
    candidate_pool_size: int = 0

    #: 本次调查的真实报告对象（不参与打分，也不进 JSON）。
    #: 保留它是为了让 runner 能渲染人类可读的诊断 —— 一个只有分数的评测
    #: 无法被复核，也无法回答"这条结论凭什么"。
    report: object | None = field(default=None, compare=False, repr=False)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("report", None)
        return payload


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
    chain_order_accuracy: float
    affected_services_accuracy: float
    # rubric 族：只统计"声明了 rubric"的场景，避免污染其他指标的分母
    wrong_answer_rate: float = 0.0
    must_identify_rate: float = 0.0
    # 排名质量：AC@k 是阶跃的，这两个对"排第几"连续敏感
    avg_at_5: float = 0.0
    mrr: float = 0.0
    mean_rank: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _attributed_service(evidence) -> str:
    """证据归属服务（复用运行时的归因逻辑，保证指标与诊断口径一致）。"""
    from traceops.orchestration.detectors import attribute_evidence

    attribution = attribute_evidence(evidence)
    return attribution[0] if attribution else (evidence.service_id or "")


def _rank_of_service(hypotheses: list, service: str) -> int | None:
    """返回命中候选服务的最高排名（rank 1 最好）。"""
    wanted = service.strip().lower()
    ranks = [
        h.rank
        for h in hypotheses
        if h.rank is not None and (h.candidate_service or "").strip().lower() == wanted
    ]
    return min(ranks) if ranks else None


def _chain_order_correct(ground_truth, report) -> bool | None:
    """传播链校验：报告里的 propagation_chain 是否复现了 ground truth 的链。

    用 propagation_chain 而不是 hypotheses —— 链上的受影响节点不该被要求有
    独立候选假设（那会让系统编造不存在的故障机制）。

    返回 None 表示该场景未声明传播链（不参与统计），避免"没声明就算对"。
    """
    expected = [hop for hop in ground_truth.propagation_chain if hop]
    if len(expected) < 2 or report is None:
        return None
    actual = [node.get("service") for node in report.propagation_chain]
    return actual == expected


def _affected_services_correct(ground_truth, report) -> bool | None:
    """受影响服务必须是链上除根因之外的服务（不多报、不漏报）。

    需要 ground truth 有传播链；否则返回 None。
    """
    chain = [hop for hop in ground_truth.propagation_chain if hop]
    if len(chain) < 3 or report is None:
        return None
    # 链上第一跳是根因，其后各跳是受影响服务
    expected = set(chain[1:])
    actual = set(report.affected_services)
    return expected == actual


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

    # 精度按"证据实际指向哪个服务"算，而不是按"谁上报了这条证据"。
    # order-service 上报的 "redis pool exhausted" 日志是关于 redis 的证据 ——
    # 若按上报者算，就会把正确的跨服务归因判成错误引用。
    wanted_service = ground_truth.service.strip().lower()
    attributable = [
        e
        for e in cited
        if e.evidence_type in expected_types
        and (
            e.evidence_type is EvidenceType.EVENT
            or _attributed_service(e).strip().lower() == wanted_service
        )
    ]
    citation_precision = len(attributable) / len(cited) if cited else 0.0

    # ---- rubric ----
    report_text = _report_text(report)
    root_cause_text = _root_cause_text(report)
    wrong_answers_asserted = _asserted_wrong_answers(root_cause_text, ground_truth.wrong_answers)
    missing_identifications = _missing_identifications(report_text, ground_truth.must_identify)

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
        chain_order_correct=_chain_order_correct(ground_truth, report),
        affected_services_correct=_affected_services_correct(ground_truth, report),
        missing_identifications=missing_identifications,
        wrong_answers_asserted=wrong_answers_asserted,
        rubric_declared=bool(ground_truth.must_identify or ground_truth.wrong_answers),
        candidate_pool_size=len({(h.candidate_service or "").strip() for h in hypotheses
                                 if (h.candidate_service or "").strip()}),
        ground_truth_rank=rank,
    )


def _root_cause_text(report) -> str:
    """「被断言为根因」的文本 —— 只取候选假设与摘要的根因从句。

    为什么必须与"被提到"分开：F-001 的报告里 `order-service` 既出现在
    摘要（"error reported by order-service"）里，也作为 rank 3 候选存在。
    前者是**归因说明**（正确且必需），后者是**候选根因**（可争议）。

    如果把两者都算作"断言了 order-service 是根因"，一个把归因说清楚的
    *好*报告会被判成错误答案 —— 这正是 aurora-lab E-9 那类"评分器 bug
    把正确行为判成失败"。所以 wrong_answers 只看真正的根因断言面。
    """
    if report is None:
        return ""
    parts: list[str] = []
    for hypothesis in getattr(report, "hypotheses", None) or []:
        # 只有 SUPPORTED 的候选才是「被断言为根因」。
        # INCONCLUSIVE / REFUTED 的候选是系统明确表达不确定，不能算成断言——
        # 否则一个诚实地把症状列为"不确定"的报告，反而会被判成说错了话。
        status = getattr(hypothesis, "status", None)
        if status is not None and getattr(status, "value", status) != "supported":
            continue
        for attr in ("candidate_service", "fault_type", "candidate_component"):
            value = getattr(hypothesis, attr, None)
            if value:
                parts.append(str(value))
        # 刻意**不含 failure_mechanism**：它是叙述性文本，天然会写
        # "error reported by order-service"——那是正确的归因说明，
        # 不是"断言 order-service 是根因"。把叙述算成断言，等于惩罚
        # 一个把归因写清楚的报告（aurora-lab E-9 那类评分器 bug）。
    summary = str(getattr(report, "summary", "") or "")
    # 摘要里 "Root cause: ..." 到第一个破折号/句号之前才是根因断言；
    # 破折号之后通常是"归因说明"，那是另一回事。
    if summary:
        clause = summary.split(" — ")[0] if " — " in summary else summary
        parts.append(clause)
    return " \n ".join(parts).lower()


def _report_text(report) -> str:
    """「被提到过」的文本 —— 供 must_identify 使用（宽松匹配）。

    与 `_root_cause_text` 的区别：这里纳入传播链节点与影响范围，
    因为它们"应当被报告点到"，但不等于是根因断言。

    仍然刻意不含 Evidence 的原始日志文本：日志里出现 "memory leak"
    是外部数据，不是系统的判断。
    """
    if report is None:
        return ""
    parts: list[str] = [_root_cause_text(report)]
    for node in getattr(report, "propagation_chain", None) or []:
        if isinstance(node, dict) and node.get("service"):
            parts.append(str(node["service"]))
    for service in getattr(report, "affected_services", None) or []:
        parts.append(str(service))
    return " \n ".join(parts).lower()


def _asserted_wrong_answers(report_text: str, wrong_answers: list[str]) -> tuple[str, ...]:
    """报告文本里出现了哪些预声明的错误答案（大小写不敏感）。"""
    asserted = [w for w in wrong_answers if w.strip().lower() in report_text]
    return tuple(asserted)


def _missing_identifications(report_text: str, must_identify: list[str]) -> tuple[str, ...]:
    """must_identify 里哪些实体在报告中完全没被提到。"""
    missing = [m for m in must_identify if m.strip().lower() not in report_text]
    return tuple(missing)


def aggregate(rows: Iterable[ScenarioOutcome]) -> AggregateMetrics:
    items = list(rows)
    if not items:
        # 空集合要产出**全零**的合法结果，而不是崩溃。
        # 此前这里按位置参数传了 12 个值给 14 字段的 dataclass → TypeError。
        # 位置参数还会在字段增删时静默错位，因此一律用关键字。
        return AggregateMetrics(
            scenario_count=0, ac_at_1=0.0, ac_at_3=0.0, ac_at_5=0.0,
            fault_type_accuracy=0.0, component_accuracy=0.0,
            diagnosis_accuracy=0.0, abstention_rate=0.0,
            evidence_recall_mean=0.0, citation_precision_mean=0.0,
            mean_tool_calls=0.0, mean_tokens=0.0,
            chain_order_accuracy=0.0, affected_services_accuracy=0.0,
            wrong_answer_rate=0.0, must_identify_rate=0.0,
            avg_at_5=0.0, mrr=0.0, mean_rank=0.0,
        )

    component_rows = [r.component_at_1 for r in items if r.component_at_1 is not None]
    # 定位与证据类指标只在"确实注入了故障"的场景上统计：
    # 健康对照组没有可定位的根因，把它算进 AC 分母会系统性低估系统能力。
    fault_rows = [r for r in items if r.fault_injected]
    # rubric 族只在"声明了 rubric"的场景上统计：
    # 没声明 rubric 的场景不该被算成"没出错"（那会虚高指标）
    rubric_rows = [r for r in items if r.rubric_declared]

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
        chain_order_accuracy=_mean(
            [1.0 if r.chain_order_correct else 0.0 for r in items if r.chain_order_correct is not None]),
        affected_services_accuracy=_mean(
            [1.0 if r.affected_services_correct else 0.0 for r in items
             if r.affected_services_correct is not None]),
        # rubric 族
        wrong_answer_rate=_mean(
            [1.0 if r.wrong_answers_asserted else 0.0 for r in rubric_rows]),
        must_identify_rate=_mean(
            [1.0 if not r.missing_identifications else 0.0 for r in rubric_rows]),
        # 排名质量：只看注入了故障的场景（与 AC@k 同分母）
        avg_at_5=average_precision_at([r.ground_truth_rank for r in fault_rows], 5),
        mrr=mean_reciprocal_rank([r.ground_truth_rank for r in fault_rows]),
        mean_rank=round(
            sum(r.ground_truth_rank for r in fault_rows if r.ground_truth_rank) /
            max(1, sum(1 for r in fault_rows if r.ground_truth_rank)), 4),
    )
