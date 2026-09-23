"""规则排序 R-001~R-006 (REVISIONS-1.1 R3)。

R3 决议：加权评分公式 S(c)=wA+wT+wP+wD+wE−wX 正式移除，P0 用确定性规则表。
规则只负责候选排序和假设状态分类，规则命中 ≠ 确定根因。

| 规则  | 判断条件                       | 结果           |
|-------|--------------------------------|----------------|
| R-001 | 候选服务没有异常观测            | 降低优先级     |
| R-002 | 候选组件在故障前出现异常        | 提高优先级     |
| R-003 | Trace 显示该组件调用异常        | 提高优先级     |
| R-004 | 存在故障前的关联配置变更        | 提高优先级     |
| R-005 | 关键预测观测与假设冲突          | 降低优先级或反驳 |
| R-006 | 没有足够证据                    | 标记为不确定   |
| R-007 | 候选给出了具体组件              | 提高优先级（信息更具体） |
"""
from __future__ import annotations

from dataclasses import dataclass, field

from traceops.domain.models import HypothesisStatus, RootCauseHypothesis

# 规则权重仅用于排序（不是概率，不输出为置信度）
_W_DEMOTE = -1
_W_ANOMALY_BEFORE = 2
_W_TRACE_ANOMALY = 2
_W_CONFIG_CHANGE = 3
_W_CONTRADICTION = -3
_W_COMPONENT_SPECIFIC = 1


@dataclass
class CandidateSignals:
    """候选假设的可观测信号。由调查管线从 Evidence 派生（二元特征，R3）。"""

    hypothesis_id: str
    has_anomaly_observation: bool = False
    anomaly_precedes_fault: bool = False
    trace_anomaly: bool = False
    config_change_precedes_fault: bool = False
    contradicting_evidence_count: int = 0
    supporting_evidence_count: int = 0
    # R-007：候选是否给出了具体组件（区别于"只知道服务"）
    has_specific_component: bool = False


@dataclass
class RankedCandidate:
    hypothesis_id: str
    score: int
    rank: int
    status: HypothesisStatus
    applied_rules: list[str] = field(default_factory=list)


def _evaluate(signals: CandidateSignals) -> tuple[int, HypothesisStatus, list[str]]:
    score = 0
    rules: list[str] = []

    # R-001 没有异常观测 → 降低优先级
    if not signals.has_anomaly_observation:
        score += _W_DEMOTE
        rules.append("R-001")

    # R-002 故障前出现异常 → 提高优先级
    if signals.anomaly_precedes_fault:
        score += _W_ANOMALY_BEFORE
        rules.append("R-002")

    # R-003 Trace 显示调用异常 → 提高优先级
    if signals.trace_anomaly:
        score += _W_TRACE_ANOMALY
        rules.append("R-003")

    # R-004 故障前的关联配置变更 → 提高优先级（最强信号：变更解释资源耗尽）
    if signals.config_change_precedes_fault:
        score += _W_CONFIG_CHANGE
        rules.append("R-004")

    # R-005 冲突证据 → 降低优先级；冲突多于支持则反驳
    if signals.contradicting_evidence_count > 0:
        score += _W_CONTRADICTION * min(signals.contradicting_evidence_count, 3)
        rules.append("R-005")

    # R-006 无任何证据 → 不确定
    no_evidence = signals.supporting_evidence_count == 0 and signals.contradicting_evidence_count == 0
    if no_evidence:
        rules.append("R-006")

    # R-007 候选给出了具体组件 → 升权。
    # 理由：能指名到 "redis-connection-pool" 的假设比只说 "redis 慢" 包含更多信息，
    # 诊断价值更高。没有这条规则时同分候选只能按 UUID 排序 = 随机，
    # 会让"服务对了但机制选错"的案例系统性失败。
    if signals.has_specific_component:
        score += _W_COMPONENT_SPECIFIC
        rules.append("R-007")

    # 状态分类（确定性）
    if signals.contradicting_evidence_count > signals.supporting_evidence_count:
        status = HypothesisStatus.REFUTED
    elif no_evidence:
        status = HypothesisStatus.INCONCLUSIVE
    elif signals.supporting_evidence_count > 0:
        status = HypothesisStatus.SUPPORTED
    else:
        status = HypothesisStatus.INVESTIGATING

    return score, status, rules


def rank_hypotheses(
    hypotheses: list[RootCauseHypothesis],
    signals: list[CandidateSignals],
    *,
    topology=None,
) -> list[RankedCandidate]:
    """确定性排序：score 降序，同分按 hypothesis_id 升序（保证可复现）。

    Args:
        topology: 提供时启用 R-008（症状不升级为根因）。None 时保持旧行为——
            宁可留一个可疑候选，也不要在没有拓扑的情况下瞎猜方向。
    """
    by_id = {s.hypothesis_id: s for s in signals}
    known = {h.id for h in hypotheses}

    unknown = [sid for sid in by_id if sid not in known]
    if unknown:
        raise ValueError(f"signal(s) for unknown hypothesis: {sorted(unknown)}")

    results: list[tuple[int, str, CandidateSignals]] = []
    for h in hypotheses:
        s = by_id.get(h.id) or CandidateSignals(hypothesis_id=h.id)
        score, _, _ = _evaluate(s)
        results.append((score, h.id, s))

    # 排序键必须完全确定，不能靠 UUID。
    # UUID 兜底等于"同分即随机"，同一场景两次运行会给出不同根因 ——
    # 这既是可复现性缺陷，也会让评估指标随测试执行顺序漂移。
    # 因此同分时依次比较：服务名 / 组件具体性 / 故障机制名 / id（最后的稳定兜底）。
    by_id_full = {h.id: h for h in hypotheses}

    def _sort_key(item: tuple[int, str, CandidateSignals]) -> tuple:
        score, hid, _ = item
        h = by_id_full[hid]
        return (
            -score,
            (h.candidate_service or "").lower(),
            h.candidate_component is None,  # False(有组件) 排前面
            (h.fault_type or "").lower(),
            hid,
        )

    results.sort(key=_sort_key)

    ranked: list[RankedCandidate] = []
    for i, (score, hid, s) in enumerate(results, start=1):
        _, status, rules = _evaluate(s)
        ranked.append(RankedCandidate(
            hypothesis_id=hid, score=score, rank=i,
            status=status, applied_rules=rules,
        ))

    _apply_symptom_rule(ranked, by_id_full, topology)
    return ranked


# 症状服务的异常可以完全由上游根因解释，因此不应被断言为独立根因。
# 用 DEGRADED 而不是 REFUTED：我们没有证据反驳"它自己也有问题"，
# 只是证据不足以支持它作为根因（防止把症状升级成第二根因）。
_SYMPTOM_STATUS = HypothesisStatus.INCONCLUSIVE


def _apply_symptom_rule(ranked: list[RankedCandidate], hypotheses: dict, topology) -> None:
    """R-008：拓扑上依赖另一个 SUPPORTED 候选的服务，其异常是症状，不是根因。

    为什么需要：`_evaluate` 只看「支持证据条数 > 0」，而症状服务的错误率
    本来就会升高（那就是症状的定义）。于是 F-001 的 rank3 会出现
    `order-service/availability_degradation` 且状态 SUPPORTED ——
    报告等于说"order-service 也是根因"，而它只是被 redis 拖累。

    判据（纯确定性，不猜）：
      候选 C 依赖 候选 R（两者都被观测到异常）→ C 的异常可由 R 解释 → C 降级。

    **不按排名先后判定**：排名可能因为平局裁决把症状排到前面（实测过），
    那样"只看前面更强的候选"会漏掉它。方向关系是结构性的，与排名无关。

    边界：
      - 互不相干的服务不降级（多故障场景必须保留多个根因）
      - topology 为 None 时整条规则不启用（没有方向信息就不猜）
      - 环检测已在拓扑构造期完成，因此不会双向降级
    """
    if topology is None:
        return

    supported = [
        (c, (hypotheses[c.hypothesis_id].candidate_service or "").strip())
        for c in ranked
        if c.status is HypothesisStatus.SUPPORTED
    ]

    for candidate, service in supported:
        if not service:
            continue
        for other, root in supported:
            if other is candidate or not root or root == service:
                continue
            try:
                depends_on_root = root in topology.dependencies(service)
            except Exception:  # 拓扑查询失败不该让排序崩掉
                depends_on_root = False
            if depends_on_root:
                candidate.status = _SYMPTOM_STATUS
                candidate.applied_rules.append(f"R-008 (symptom of {root}, not a root cause)")
                break

    # 降级后重排：断言为根因的候选必须排在"受影响/不确定"之前。
    # 否则 rank1 可能是被降级的症状服务，AC@1 会被这种排序噪声拖低。
    ranked.sort(key=lambda r: (r.status is not HypothesisStatus.SUPPORTED, r.rank))
    for i, candidate in enumerate(ranked, start=1):
        candidate.rank = i


def derive_signals(
    hypothesis: RootCauseHypothesis,
    links: list,
    store,
    *,
    fault_start,
) -> CandidateSignals:
    """从 证据-假设 关联派生二元信号（R3: 二元特征 + 确定性规则）。

    语义约定（P0）:
    - SUPPORTS 关联 = 该证据被视为异常的观测（link 由规则/模型/人工创建时已判断）
    - 仅当证据服务属于候选服务时才算候选自身异常观测
    - EVENT 证据 = 变更事件；observed_at 早于故障开始即视为"故障前变更"
    """
    from traceops.domain.models import EvidenceType, RelationType

    by_id = {}
    for link in links:
        if link.hypothesis_id != hypothesis.id:
            continue
        ev = by_id.get(link.evidence_id)
        if ev is None:
            try:
                ev = store.get(link.evidence_id)
            except KeyError:
                continue
            by_id[link.evidence_id] = ev

    supports = [by_id[l.evidence_id] for l in links
                if l.hypothesis_id == hypothesis.id
                and l.relation_type == RelationType.SUPPORTS
                and l.evidence_id in by_id]
    # 矛盾计数必须与支持计数**同分母**：都只算"证据确实存在于库"的 link。
    #
    # 旧写法只看 link、不查证据是否存在，于是引用了幽灵证据的 CONTRADICTS
    # 会被计入，而它的支持对应物会被跳过 —— 分母不一致。
    # 后果很具体：`REFUTED` 的判据是「矛盾数 > 支持数」，
    # 一条脏 link 就能把假设判成 REFUTED，而它引用的证据根本不在库里。
    # 那是"证据驱动"原则的反面：结论来自无法核验的引用。
    contradicts = [by_id[l.evidence_id] for l in links
                   if l.hypothesis_id == hypothesis.id
                   and l.relation_type == RelationType.CONTRADICTS
                   and l.evidence_id in by_id]

    observed_types = {EvidenceType.METRIC, EvidenceType.LOG, EvidenceType.TRACE}
    own_anomalies = [
        ev for ev in supports
        if ev.evidence_type in observed_types
        and (ev.service_id is None or ev.service_id == hypothesis.candidate_service)
    ]
    before_fault = [ev for ev in supports if ev.observed_at < fault_start]

    return CandidateSignals(
        hypothesis_id=hypothesis.id,
        has_specific_component=bool(hypothesis.candidate_component),
        has_anomaly_observation=bool(own_anomalies),
        anomaly_precedes_fault=bool(before_fault),
        trace_anomaly=any(ev.evidence_type == EvidenceType.TRACE for ev in supports),
        config_change_precedes_fault=bool(
            [ev for ev in supports
             if ev.evidence_type == EvidenceType.EVENT and ev.observed_at < fault_start]
        ),
        contradicting_evidence_count=len(contradicts),
        supporting_evidence_count=len(supports),
    )
