"""RCA 报告生成 + 引用完整性闸门 (REVISIONS-1.1 成本低直接进 P0)。

报告只包含两类内容:
1. 结构化结论（诊断状态 / 摘要 / 缺失数据）
2. 对证据与假设的引用（全部 ID 必须真实存在于 Evidence Store）
"""
from __future__ import annotations

from datetime import datetime

from traceops.domain.models import (
    HypothesisEvidence,
    HypothesisStatus,
    RCAReport,
    RootCauseHypothesis,
)
from traceops.rca.ranking import RankedCandidate


class ReportIntegrityError(Exception):
    """报告引用了不存在的证据 / 跨事故证据。拒绝出报告。"""


def build_report(
    *,
    investigation_run_id: str,
    hypotheses: list[RootCauseHypothesis],
    links: list[HypothesisEvidence],
    evidence: list,
    ranked: list[RankedCandidate],
    fault_start: datetime,
    store,
    missing_data: list[str] | None = None,
    affected_services: list[str] | None = None,
    correlation_notes: list[str] | None = None,
    propagation_chain: list | None = None,
) -> RCAReport:
    """组装并校验诊断报告。

    - 排序结果回写到假设的 rank/status
    - 诊断状态: 首选 SUPPORTED → SUPPORTED; 首选与次选同分 → INCONCLUSIVE（歧义）
      全部 REFUTED → REFUTED; 否则 INCONCLUSIVE
    - INCONCLUSIVE / REFUTED 视为主动放弃确定性诊断 (abstained=True)
    - 生成后必须通过引用完整性校验，否则抛 ReportIntegrityError
    """
    rank_by_id = {r.hypothesis_id: r for r in ranked}
    updated_hypotheses: list[RootCauseHypothesis] = []
    for h in hypotheses:
        r = rank_by_id.get(h.id)
        if r is not None:
            h.rank = r.rank
            h.status = r.status
        updated_hypotheses.append(h)

    # 报告内假设按 rank 排序：rank 1 在最前，消费方无需再排序
    updated_hypotheses.sort(key=lambda h: (h.rank is None, h.rank or 0))

    top = ranked[0] if ranked else None
    # 歧义闸门：多个候选同分时，选谁是任意的。此时给出单一根因就是过度断言，
    # 故意降级为 INCONCLUSIVE，并把候选一并写进摘要（R7：不确定性进流程）。
    tied = (
        [r for r in ranked if r.status == HypothesisStatus.SUPPORTED and r.score == top.score]
        if top is not None and top.status == HypothesisStatus.SUPPORTED
        else []
    )
    # 歧义只在"**不同服务**并列"时成立。
    #
    # 同一服务的多个故障机制并列（redis 既是 pool_exhaustion 又是 latency_degradation）
    # 不是歧义：服务定位已经确定，只是解释待细化。把它判成 INCONCLUSIVE 会让
    # 本来明确的根因服务被降级，等于用"机制不确定"否定了"服务确定"。
    # 正确做法：服务确定就报服务，机制并列写进摘要作为待细化项。
    tied_services = {
        next((h.candidate_service for h in updated_hypotheses if h.id == r.hypothesis_id), None)
        for r in tied
    }
    tied_services.discard(None)
    if len(tied) > 1 and len(tied_services) > 1:
        names = [
            f"{h.candidate_service}/{h.fault_type}"
            for h in updated_hypotheses
            if h.id in {r.hypothesis_id for r in tied}
        ]
        diagnosis = "INCONCLUSIVE"
        summary = (
            f"Ambiguous evidence: {len(tied)} candidate root causes across "
            f"{len(tied_services)} services ({', '.join(sorted(tied_services))}) are equally "
            f"supported ({', '.join(names)}); no rule discriminates them"
        )
    elif top is not None and top.status == HypothesisStatus.SUPPORTED:
        diagnosis = "SUPPORTED"
        lead = next((h for h in updated_hypotheses if h.id == top.hypothesis_id), None)
        if lead is not None:
            parts = [lead.candidate_service]
            if lead.candidate_component:
                parts.append(lead.candidate_component)
            parts.append(f"({lead.fault_type})")
            summary = "Root cause: " + " ".join(parts) + f" — {lead.failure_mechanism}"
        else:
            summary = "Root cause identified"
    elif ranked and all(r.status == HypothesisStatus.REFUTED for r in ranked):
        # REFUTED 的合法前提：我们确实有观测，且观测否定了候选。
        # 若采集过程本身有缺失（数据源不可达 / 查询空结果），那"什么都没看见"
        # 不能冒充"已排除" —— 那正是把 absence 当成阴性证据。
        if missing_data:
            diagnosis = "INCONCLUSIVE"
            summary = (
                "All candidates would be refuted, but data collection was incomplete "
                "(" + "; ".join(list(missing_data)[:3]) + "); "
                "a refutation requires observations we do not have"
            )
        else:
            diagnosis = "REFUTED"
            summary = "All candidate root causes were refuted by contradictory evidence"
    else:
        diagnosis = "INCONCLUSIVE"
        summary = "Insufficient evidence for a conclusive diagnosis"

    if correlation_notes:
        summary = summary.rstrip(".") + ". " + "; ".join(correlation_notes) + "."

    report = RCAReport(
        investigation_run_id=investigation_run_id,
        diagnosis_status=diagnosis,
        summary=summary,
        hypotheses=updated_hypotheses,
        hypothesis_evidence=links,
        evidence=list(evidence),
        affected_services=list(affected_services or []),
        propagation_chain=[node.to_dict() for node in (propagation_chain or [])],
        missing_data=missing_data or [],
        abstained=diagnosis != "SUPPORTED",
    )

    validation = store.validate_report_references(report)
    if not validation.passed:
        raise ReportIntegrityError(
            "report references invalid evidence: " + ", ".join(validation.invalid_ids or validation.errors)
        )
    return report
