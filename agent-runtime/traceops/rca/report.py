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


def _independent_faults(services: set[str], topology) -> bool:
    """这些服务之间是否**互不相干**（各自独立的故障）。

    操作定义：任意两方都不同时满足下面任一条件，则视为互不相干：
      - 一方在另一方的下游（dependencies 传递闭包）
      - 一方在另一方的上游（dependents 传递闭包）
      - 两者共享同一个依赖（兄弟）

    topology 为 None 时返回 False —— 没有方向信息就不猜，
    保持旧的歧义闸门行为（宁可弃权，也不要凭空宣布多根因）。
    """
    if topology is None or len(services) < 2:
        return False
    names = sorted(services)

    def _related(a: str, b: str) -> bool:
        try:
            downstream_a = topology.expand_scope(a, depth=3)
            downstream_b = topology.expand_scope(b, depth=3)
            shared_deps = topology.dependencies(a) & topology.dependencies(b)
        except Exception:
            return True  # 拓扑查询失败时保守：当作相干，维持弃权
        return b in downstream_a or a in downstream_b or bool(shared_deps)

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if _related(a, b):
                return False
    return True


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
    no_data: list[str] | None = None,
    affected_services: list[str] | None = None,
    correlation_notes: list[str] | None = None,
    propagation_chain: list | None = None,
    incident_id: str | None = None,
    profile_digest: str | None = None,
    semantic_checks: dict | None = None,
    topology=None,
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
    # 同分但在拓扑上**互不相干** → 这不是"分不清哪个是根因"，
    # 而是"两个故障同时发生"。把独立故障判成歧义会让真实的第二个故障
    # 被报告说成"不存在"（aurora-lab 场景 12 的原话：
    # "A real second outage went unreported, and the report said it was not there"）。
    independent = _independent_faults(tied_services, topology)
    if len(tied) > 1 and len(tied_services) > 1 and not independent:
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
        lead = next((h for h in updated_hypotheses if h.id == top.hypothesis_id), None)
        # 语义闸门：覆盖率太低意味着"这个机制声称的观测大多没出现"。
        # 例如声称 latency_degradation 却没有任何慢 span —— 预期证据缺失本身
        # 就是证据（aurora conclusion_gate: absence of EXPECTED evidence is evidence）。
        # 只在覆盖率为 0 时降级：部分覆盖是正常的（真实故障不必凑齐三件套），
        # 一刀切要求高覆盖率会把本来对的诊断降级成不确定。
        semantic = semantic_checks.get(top.hypothesis_id) if semantic_checks else None
        if semantic is not None and semantic.expected and semantic.coverage == 0.0:
            diagnosis = "INCONCLUSIVE"
            summary = (
                f"Candidate {lead.candidate_service}/{lead.fault_type} is supported by evidence, "
                f"but none of its expected observations were seen "
                f"(expected: {', '.join(semantic.expected)}); "
                "absence of expected evidence is not confirmation"
                if lead is not None else
                "Supported candidate lacks all expected observations"
            )
        else:
            diagnosis = "SUPPORTED"
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
        no_data=no_data or [],
        abstained=diagnosis != "SUPPORTED",
        root_cause_time=_root_cause_time(updated_hypotheses, links, evidence),
        profile_digest=profile_digest,
    )

    # 必须绑定本次事故：只查"ID 在 Store 里存在"会放过**引用了别处证据**的报告。
    # 评测时（同一进程多场景）尤其危险：ID 确实存在，但那份证据属于别的场景，
    # 于是"引用完整性通过、证据却是别人的"。
    validation = store.validate_report_references(report, incident_id=incident_id)
    if not validation.passed:
        # 用 errors 而不是只列 ID：调用方需要知道是"不存在"还是"属于别的事故"，
        # 这两种情况的修法完全不同。
        detail = "; ".join(validation.errors) or ", ".join(validation.invalid_ids)
        raise ReportIntegrityError(f"report references invalid evidence: {detail}")
    return report


def _root_cause_time(
    hypotheses: list[RootCauseHypothesis],
    links: list,
    evidence_list: list,
) -> datetime | None:
    """根因开始时间 = rank1 候选**支持证据**里最早的 observed_at。

    为什么必须来自证据而不是请求里的 fault_start：
    `fault_start` 是告警**声称**的时间（假设），`observed_at` 是我们实际
    **观测到**的时间（事实）。报告只陈述事实 —— 拿 fault_start 充数会让
    "证据可信"这条原则失效，事后复盘也查不出报告其实没验证过时间。

    为什么取"最早"而不是"最晚"：根因是故障的**起点**，
    最晚的证据只能说明影响持续到那时。

    没有 rank1、没有支持证据、或支持证据不在证据集里 → 返回 None。
    """
    ranked_top = next((h for h in hypotheses if (h.rank or 0) == 1), None)
    if ranked_top is None:
        return None

    # 只取"支持"关系的证据 ID；反驳/无关的证据不能用来定根因时间
    supported_ids: set[str] = set()
    for link in links:
        hypothesis_id = getattr(link, "hypothesis_id", None)
        relation = getattr(link, "relation_type", None)
        relation_value = getattr(relation, "value", relation)
        if hypothesis_id != ranked_top.id:
            continue
        if str(relation_value).upper() == "SUPPORTS":
            evidence_id = getattr(link, "evidence_id", None)
            if evidence_id:
                supported_ids.add(evidence_id)

    if not supported_ids:
        return None

    times = [
        item.observed_at
        for item in evidence_list
        if item.id in supported_ids and item.observed_at is not None
    ]
    return min(times) if times else None
