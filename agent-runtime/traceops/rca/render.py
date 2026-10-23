"""把 `RCAReport` 渲染成人读的四段，而不是让人读 8KB JSON。

## 为什么需要这一层

报告对象是给程序用的：23 个键、每条证据带 checksum 与窗口。它对下游系统很友好，
对**下一个要看它的人**不友好 —— 而那个人（值班的工程师）恰恰是产品的用户。

所以这里只有一件事：把"结论 / 为什么信 / 下一步 / 不确定的地方"这四句话
从结构化数据里取出来，一句一句写清楚。

## 一条不能破的规则

**渲染只搬运，不推理。** 这里不允许出现报告对象里没有的判断：

- 结论只复述 rank1 假设与 `diagnosis_status`；
- "为什么信"只列 `hypothesis_evidence` 里 `SUPPORTED` 的边，逐条附它自己的数字；
- "不确定"只复述 `missing_data` / `degraded_reads` / `no_data` / `topology_drift`；
- "下一步"是**按 fault_type 的固定建议表**，不是对本例的新推断（表里每条都在说
  "这类机制通常怎么查"），并且明确标注它是建议而不是结论。

一旦渲染层开始"补一句解释"，报告与摘要就会分叉：审计时看的是 JSON，人看的是这段
文字，两者不一致的那一刻，产品就开始撒谎了。
"""
from __future__ import annotations

from typing import Any

from traceops.domain.models import EvidenceType, HypothesisStatus, RCAReport
from traceops.orchestration.detectors import ERROR_RATE_THRESHOLD

#: 按机制给出的**通用**排查方向。它不针对这一次的实例，所以措辞统一是"通常"。
#: 新增 fault_type 时必须在这里补一条，否则渲染会明说"没有对应建议"而不是编一个。
_NEXT_STEPS: dict[str, tuple[str, ...]] = {
    "pool_exhaustion": (
        "看连接池上限与当前活跃连接数：是打满而不是泄漏时，先加额度；持续增长才是泄漏",
        "看同一依赖的其他调用方是否同时变慢（共享池被别的服务吃掉了）",
    ),
    "latency_degradation": (
        "看下游 p99 与慢查询/慢命令的分布，区分「整体变慢」与「少数请求卡住」",
        "确认这一层的耗时是自己算的，还是在等下一跳",
    ),
    "availability_degradation": (
        "看错误日志里的第一条与最早时间点，而不是最后一条",
        "看是否伴随重启/发布/扩缩容事件",
    ),
    "dependency_timeout": (
        "看超时配置与实际 RTT 的比值：是配置太小，还是真的不通",
        "看是否只在特定实例/可用区出现",
    ),
}

#: 机制的中文说法。`failure_mechanism` 是生成假设时的自由文本（英文、面向审计），
#: 给人看的这一层换成一句能读懂的话；未知机制保留原文，不要编。
_MECHANISM_LABEL = {
    "pool_exhaustion": "连接池耗尽：连接都被占住，请求在等资源",
    "latency_degradation": "响应变慢：延迟显著高于平时",
    "availability_degradation": "可用性下降：错误率显著上升",
    "dependency_timeout": "依赖超时：调用下游超时",
}

#: 证据类型的中文名。用于"为什么信"每行的前缀。
_KIND_LABEL = {
    EvidenceType.METRIC: "指标",
    EvidenceType.LOG: "日志",
    EvidenceType.TRACE: "追踪",
    EvidenceType.EVENT: "变更",
    EvidenceType.PROFILE: "火焰图",
}

_STATUS_LABEL = {
    "SUPPORTED": "已确认",
    "REFUTED": "已推翻",
    "INCONCLUSIVE": "无法确定",
}


def render_report(
    report: RCAReport,
    *,
    cost: dict[str, Any] | None = None,
    max_evidence: int = 4,
    max_uncertainty: int = 5,
) -> str:
    """渲染成四段 + 成本行。返回的字符串可以直接贴进工单或聊天窗口。"""
    blocks = [
        _conclusion(report),
        _why(report, max_evidence=max_evidence),
        _next_steps(report),
        _uncertainty(report, max_uncertainty=max_uncertainty),
        _cost(report, cost),
    ]
    return "\n\n".join(block for block in blocks if block)


def _conclusion(report: RCAReport) -> str:
    status = _STATUS_LABEL.get(report.diagnosis_status, report.diagnosis_status)
    top = _rank_one(report)
    if top is None:
        return "结论\n  无法确定：这一轮没有产生任何根因候选。"

    where = top.candidate_service
    if top.candidate_component:
        where = f"{where} 的 {top.candidate_component}"
    mechanism = _MECHANISM_LABEL.get(top.fault_type, top.failure_mechanism)
    line = f"结论\n  {status}：{where} —— {mechanism}（{top.fault_type}）"

    supports = [
        h for h in report.hypothesis_evidence
        if h.hypothesis_id == top.id and h.relation_type.value == "SUPPORTS"
    ]
    if report.diagnosis_status == "SUPPORTED":
        line += f"\n  依据：{len(supports)} 条独立证据支持它"
    elif report.diagnosis_status == "INCONCLUSIVE":
        line += "\n  依据：证据不足以支持任何单一结论（下面写了缺什么）"
    else:
        line += "\n  依据：证据与这个候选矛盾"

    if report.root_cause_time is not None:
        line += f"\n  最早异常时间：{report.root_cause_time.isoformat()}（观测到的，不是告警声称的）"
    if report.affected_services:
        line += f"\n  受影响：" + "、".join(report.affected_services)
    return line


def _why(report: RCAReport, *, max_evidence: int) -> str:
    top = _rank_one(report)
    if top is None:
        return ""
    by_id = {ev.id: ev for ev in report.evidence}
    lines: list[str] = []
    for link in report.hypothesis_evidence:
        if link.hypothesis_id != top.id or link.relation_type.value != "SUPPORTS":
            continue
        evidence = by_id.get(link.evidence_id)
        if evidence is None:
            # 报告完整性闸门应该已经拦掉这种情况；真出现时说出来，不要假装有一条证据。
            lines.append(f"  · 引用了不存在的证据 {link.evidence_id}（不该发生）")
            continue
        lines.append("  · " + _describe_evidence(evidence))
        if len(lines) >= max_evidence:
            break
    if not lines:
        return "为什么信\n  没有支持证据（这就是状态不是「已确认」的原因）"
    return "为什么信\n" + "\n".join(lines)


def _describe_evidence(evidence: Any) -> str:
    kind = _KIND_LABEL.get(evidence.evidence_type, str(evidence.evidence_type))
    data = evidence.normalized_data or {}
    when = evidence.observed_at.isoformat() if evidence.observed_at else "未知时间"
    detail = _detail(evidence.evidence_type, data)
    head = f"{kind}｜{evidence.service_id}｜{when}"
    return f"{head}\n    {detail}" if detail else head


def _detail(kind: EvidenceType, data: dict[str, Any]) -> str:
    """从归一化数据里取一行可引用的数字。

    形状由 `traceops/evidence/normalizer.py` 决定，**不是**这里决定的：metric 是
    `series[{metric, values}]`，log 是 `records[{timestamp, message, labels}]`，
    trace 是 `spans[{operation_name, duration_us}]`，event 是
    `events[{event: {...payload}, timestamp}]`。取不到的字段直接不写，不要用占位符
    把"没有这个数"伪装成"这个数是 0"。
    """
    if kind is EvidenceType.METRIC:
        peak = _metric_peak(data)
        if peak is None:
            return ""
        exceeded = peak >= ERROR_RATE_THRESHOLD
        return f"峰值 {peak:.3f}" + ("（超过阈值）" if exceeded else "（未超阈值）")

    if kind is EvidenceType.LOG:
        hits = _log_keyword_hits(data)
        records = data.get("records") or []
        sample = ""
        if isinstance(records, list) and records and isinstance(records[0], dict):
            sample = str(records[0].get("message") or "")[:120]
        head = f"{len(records)} 条记录" if isinstance(records, list) else "记录"
        if hits:
            head += f"，命中机制关键词：{'、'.join(hits[:4])}"
        return head + (f"；例如：{sample}" if sample else "")

    if kind is EvidenceType.TRACE:
        spans = data.get("spans") or []
        if not isinstance(spans, list) or not spans:
            return ""
        slowest = max(
            (sp for sp in spans if isinstance(sp, dict)),
            key=lambda sp: sp.get("duration_us") or 0,
            default=None,
        )
        if slowest is None:
            return ""
        duration_ms = (slowest.get("duration_us") or 0) / 1000
        op = slowest.get("operation_name") or "未知操作"
        return f"{len(spans)} 个 span，最慢 {op} {duration_ms:.0f}ms"

    if kind is EvidenceType.EVENT:
        events = data.get("events") or []
        first = _first_event(events)
        if first is None:
            return "变更事件"
        kind_text = first.get("change_kind") or first.get("type") or "变更"
        summary = first.get("summary") or first.get("revision") or ""
        when = first.get("changed_at") or first.get("timestamp") or ""
        bits = [b for b in (str(kind_text), str(when), str(summary)) if b]
        return "｜".join(bits)[:180]

    if kind is EvidenceType.PROFILE:
        frames = data.get("hot_frames") or []
        if isinstance(frames, list) and frames and isinstance(frames[0], dict):
            return f"最热帧 {frames[0].get('frame')}（self {frames[0].get('self_value')}）"
        return ""
    return ""


def _first_event(events: Any) -> dict[str, Any] | None:
    """EventNormalizer 把载荷放在 `events[i]["event"]` 里，时间戳在外面。"""
    if not isinstance(events, list) or not events:
        return None
    item = events[0]
    if not isinstance(item, dict):
        return None
    inner = item.get("event")
    if isinstance(inner, dict):
        merged = dict(inner)
        merged.setdefault("timestamp", item.get("timestamp"))
        return merged
    return item


def _metric_peak(data: dict[str, Any]) -> float | None:
    """与 `_evidence_summary` 用同一份语义：取所有序列的最大采样值。

    直接复用编排层那个函数，而不是在这里再写一遍 —— 两处各算一遍的结果迟早不一致，
    而不一致的那一刻报告与摘要会开始互相矛盾。
    """
    from traceops.orchestration.graph import _metric_peak as peak

    return peak(data)


def _log_keyword_hits(data: dict[str, Any]) -> list[str]:
    from traceops.orchestration.graph import _log_keyword_hits as hits

    return hits(data)


def _next_steps(report: RCAReport) -> str:
    top = _rank_one(report)
    if top is None:
        return "下一步\n  先解决取证：见下面的「不确定的地方」。"
    steps = list(_NEXT_STEPS.get(top.fault_type, ()))
    change = _latest_change(report)
    if change is not None:
        steps.append(f"先看这次变更：{change}（它早于最早异常，因果未排除）")
    if not steps:
        steps.append(f"这一类机制（{top.fault_type}）还没有通用建议，只能按证据逐条查")
    body = "\n".join(f"  {i}. {step}" for i, step in enumerate(steps, 1))
    return "下一步（建议，不是结论）\n" + body


def _latest_change(report: RCAReport) -> str | None:
    """找出报告里最早的一条变更事件，用来提示"先看变更"。

    只取**证据里真实存在**的变更；没有就不提，不要凭 fault_type 猜一个。
    """
    changes = [
        ev for ev in report.evidence if ev.evidence_type is EvidenceType.EVENT
    ]
    if not changes:
        return None
    change = min(changes, key=lambda ev: ev.observed_at)
    first = _first_event((change.normalized_data or {}).get("events"))
    when = change.observed_at.isoformat() if change.observed_at else "未知时间"
    if first is None:
        return when
    text = first.get("summary") or first.get("revision")
    return f"{when} {text}"[:200] if text else when


def _uncertainty(report: RCAReport, *, max_uncertainty: int) -> str:
    """三层诚实：没查到（没法查）/ 查了没有（阴性观测）/ 降级（有数据但读不全）。

    三者方向相反，所以分开写、不合并 —— 把"没看见"和"看见了没有"混在一句里，
    读的人就无法判断该不该相信结论。
    """
    lines: list[str] = []
    missing = list(report.missing_data)
    # 拓扑里"声明了但本窗口没走动"的边，编排层已经写进 missing_data；
    # 这里不重复第二遍（同一件事说两次，读者会以为有两件事）。
    drift = report.topology_drift or {}

    for item in missing[:max_uncertainty]:
        lines.append(f"  · 没查到：{item}")
    if len(missing) > max_uncertainty:
        lines.append(f"  · ……另有 {len(missing) - max_uncertainty} 条没查到（见 JSON 的 missing_data）")

    empty = list(report.no_data)
    for item in empty[:3]:
        lines.append(f"  · 查了但没有：{item}")
    if len(empty) > 3:
        lines.append(f"  · ……另有 {len(empty) - 3} 条查询成功但为空（这不等于故障不存在）")

    for item in report.degraded_reads:
        read = item.get("read") or "unavailable"
        lines.append(
            f"  · 降级读取：{item.get('tool')}（{item.get('kind')}）—— 完整数据看 {read}"
        )

    for edge in drift.get("undeclared_edges") or []:
        lines.append(f"  · 拓扑漏了这条调用边：{' → '.join(edge)}（爆炸半径可能算少）")
    if report.priors:
        lines.append(f"  · 用了 {len(report.priors)} 条历史先验（只调权，不作为结论）")
    if report.cache_hits:
        lines.append(
            f"  · {len(report.cache_hits)} 条证据来自查询缓存（保留了原始取数时刻）"
        )
    if not lines:
        if report.evidence:
            return "不确定的地方\n  无：所有计划内查询都成功了，报告里的每条证据都是本窗口观测到的。"
        # 报告里既无缺失、也无证据：不能据此宣称"查询都成功了" —— 我们只知道没人
        # 记录过查询结果。断言"没问题"比承认"不知道"危险得多。
        return "不确定的地方\n  这份报告没有证据也没有查询记录：无从判断取证是否完整。"
    return "不确定的地方\n" + "\n".join(lines)


def _cost(report: RCAReport, cost: dict[str, Any] | None) -> str:
    parts = [f"证据 {len(report.evidence)} 条", f"候选 {len(report.hypotheses)} 个"]
    if cost:
        if "tool_calls" in cost:
            parts.append(f"数据源查询 {cost['tool_calls']} 次")
        if cost.get("tokens_used"):
            parts.append(f"tokens {cost['tokens_used']}")
        else:
            parts.append("未使用模型")
    return "成本\n  " + "｜".join(parts)


def _rank_one(report: RCAReport) -> Any | None:
    """rank1 候选。没有 rank 时按候选顺序取第一个 —— 报告里 rank 由排序节点写入。"""
    ranked = [h for h in report.hypotheses if h.rank is not None]
    if ranked:
        return min(ranked, key=lambda h: h.rank)
    return report.hypotheses[0] if report.hypotheses else None


def render_hypotheses(report: RCAReport) -> str:
    """候选一览：让人看到"被排除的另外几个是什么"。"""
    if not report.hypotheses:
        return "候选\n  无"
    rows = sorted(report.hypotheses, key=lambda h: (h.rank is None, h.rank or 0))
    lines = ["候选（按排名）"]
    for h in rows:
        mark = _STATUS_LABEL_MARK.get(h.status.value, h.status.value)
        rank = f"#{h.rank}" if h.rank is not None else "-"
        where = h.candidate_service + (f" / {h.candidate_component}" if h.candidate_component else "")
        lines.append(f"  {rank} {where} — {h.fault_type} [{mark}]")
    return "\n".join(lines)


_STATUS_LABEL_MARK = {
    HypothesisStatus.SUPPORTED.value: "支持",
    HypothesisStatus.REFUTED.value: "推翻",
    HypothesisStatus.INCONCLUSIVE.value: "不确定",
    HypothesisStatus.PROPOSED.value: "待验",
}
