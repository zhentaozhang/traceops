"""确定性异常检测与假设生成（P0）。

设计契约：
- 检测规则只依赖证据的 `query_template_id` 与归一化内容，不读原始 payload。
- 每个假设都是"可被证据支持或反驳的陈述"，不是对根因的宣告。
- P0 不调用 LLM；`ModelClient` 接口为 P1 的 LLM 假设生成预留。

归一化输出形状（见 evidence/normalizer.py，勿凭直觉）：
- metric: {"series": [{"metric": {...}, "values": [[ts, float], ...]}]}
- log:    {"records": [{"message", "labels": {"level": ...}, "timestamp", "trace_id"}]}
- trace:  {"spans": [{"duration_us": int, "operation_name": str, "service": str, ...}]}
- event:  {"events": [{"timestamp", "component", ...}]}
"""
from __future__ import annotations

import re

from traceops.domain.models import (
    Evidence,
    EvidenceType,
    HypothesisStatus,
    RootCauseHypothesis,
)

# 阈值（P0 固定值；P1 应来自数据分布基线）
ERROR_RATE_THRESHOLD = 0.05
SLOW_SPAN_THRESHOLD_SECONDS = 0.5

_ERROR_HINTS = ("error", "exception", "timeout", "timed out", "pool", "refused", "unavailable")

# 每种故障机制"应该会看到什么"。与 rca/semantic.py 的 EXPECTATIONS 同源，
# 在这里声明是为了让假设自带可核对的预期，而不是留一个空字段。
_EXPECTED_BY_FAULT: dict[str, tuple[str, ...]] = {
    "availability_degradation": ("error_rate_rise", "error_logs"),
    "pool_exhaustion": ("error_rate_rise", "error_logs", "slow_span"),
    "latency_degradation": ("slow_span",),
    "dependency_timeout": ("error_logs", "error_rate_rise"),
}


def metric_is_anomalous(evidence: Evidence) -> bool:
    """仅对 error-rate 模板应用阈值，避免把任意指标序列当异常。"""
    if evidence.evidence_type is not EvidenceType.METRIC:
        return False
    if "error-rate" not in evidence.query_template_id and "error_rate" not in evidence.query_template_id:
        return False
    for series in evidence.normalized_data.get("series", []):
        for point in series.get("values", []):
            try:
                if float(point[1]) >= ERROR_RATE_THRESHOLD:
                    return True
            except (TypeError, ValueError, IndexError):
                continue
    return False


def logs_are_anomalous(evidence: Evidence) -> bool:
    if evidence.evidence_type is not EvidenceType.LOG:
        return False
    for record in evidence.normalized_data.get("records", []):
        labels = record.get("labels") or {}
        blob = f"{record.get('message', '')} {labels.get('level', '')}".lower()
        if any(hint in blob for hint in _ERROR_HINTS):
            return True
    return False


def traces_are_anomalous(evidence: Evidence) -> bool:
    if evidence.evidence_type is not EvidenceType.TRACE:
        return False
    for span in evidence.normalized_data.get("spans", []):
        try:
            if float(span.get("duration_us") or 0) / 1_000_000 >= SLOW_SPAN_THRESHOLD_SECONDS:
                return True
        except (TypeError, ValueError):
            continue
    return False


# 日志里出现的依赖关键词 → 该依赖的组件名规则
_DEPENDENCY_KEYWORDS = ("redis", "mysql", "kafka", "billing-api", "postgres", "rabbitmq")

_COMPONENT_RULES = {
    "redis": "redis-connection-pool",
    "mysql": "mysql-connection-pool",
    "postgres": "postgres-connection-pool",
}


def _attribute_dependency(
    message: str,
    reporting_service: str,
    topology: object | None,
) -> tuple[str, str | None] | None:
    """从"谁报的日志 + 日志提到谁"推断出**真正出问题的服务**。

    关键修正：order-service 的日志里写 "redis timeout / pool exhausted"，
    说明出问题的是 redis，不是 order-service 拥有一个池。

    返回 (owner_service, owner_component) 或 None。
    """
    lowered = message.lower()
    for keyword in _DEPENDENCY_KEYWORDS:
        if keyword not in lowered:
            continue
        # 拓扑可用时，只有 keyword 确实是 reporting_service 的依赖才归因给它
        if topology is not None:
            deps = topology.dependencies(reporting_service)
            if keyword not in deps:
                continue
        component = None
        if "pool" in lowered:
            component = _COMPONENT_RULES.get(keyword, f"{keyword}-connection-pool")
        return keyword, component
    return None


def _extract_component(message: str) -> str | None:
    """只为"本服务自身组件"场景兜底（不涉及依赖归因时使用）。"""
    match = re.search(r"\b([a-z][a-z0-9-]+-connection-pool)\b", message.lower())
    return match.group(1) if match else None


def attribute_evidence(evidence: Evidence, topology: object | None = None) -> tuple[str, str | None] | None:
    """一条证据"指向哪个服务"（证据归属的唯一事实来源）。

    - 日志/链路：若文本提到 reporting_service 的依赖，归属该依赖；否则归属 reporting service
    - 指标/事件：归属 reporting service（指标是服务自身状态，事件是变更对象）
    返回 (owner_service, owner_component) 或 None（无归属）。
    """
    if evidence.service_id is None:
        return None
    if evidence.evidence_type in (EvidenceType.LOG, EvidenceType.TRACE):
        texts: list[str] = []
        if evidence.evidence_type is EvidenceType.LOG:
            texts = [str(r.get("message", "")) for r in evidence.normalized_data.get("records", [])]
        else:
            texts = [str(s.get("operation_name", "")) for s in evidence.normalized_data.get("spans", [])]
        for text in texts:
            hit = _attribute_dependency(text, evidence.service_id, topology)
            if hit is not None:
                return hit
    return evidence.service_id, None


def generate_hypotheses(
    evidence_list: list[Evidence],
    *,
    investigation_run_id: str,
    topology: object | None = None,
    mechanism_only_services: set[str] | None = None,
) -> list[RootCauseHypothesis]:
    """从证据生成候选假设（确定性）。同一 (service, fault_type, component) 只生成一次。

    Args:
        mechanism_only_services: 允许**描述机制**但**不允许新增候选**的服务集合。

            这是"影响层证据"的正确用法。影响层的证据（上游调用方 / 兄弟节点）
            不能生成候选根因，否则会把症状服务当根因；但 `attribute_evidence`
            可能把它归因到**某个已经存在的候选服务**（fiber 服务的日志讲 redis，
            那就是关于 redis 的证据），这种证据本身是机制判定的宝贵材料。

            判据是**归因结果**而不是证据来源：只有当影响层证据被归因到一个
            已存在的候选服务时，它才参与机制描述。归因不到的证据仍然被排除。
    """
    # 去重键 = (service, fault_type)。**不含 component**：
    # 同一条慢链路可能从日志推出 "redis-connection-pool"、从 span 推出 None，
    # 那是同一种故障机制的两种描述，不该算成两个候选。否则候选集被同一服务灌满，
    # 触发歧义闸门 → 把本来明确的根因降级成 INCONCLUSIVE。
    # component 作"最具体者胜"合并：先有的保留，后来更具体的覆盖。
    seen: dict[tuple[str, str], RootCauseHypothesis] = {}
    hypotheses: list[RootCauseHypothesis] = []

    def _add(service: str, fault_type: str, component: str | None, mechanism: str,
             *, reported_by: str | None = None) -> None:
        # 机制-only 服务：允许为**已存在的候选**补充机制，但不得新增候选。
        # 例：兄弟服务 inventory-service 的日志讲 redis timeout →
        # `attribute_evidence` 把它归因给 redis → 它可以描述 redis 的机制，
        # 但不能让 inventory-service 自己变成候选根因。
        if mechanism_only_services and reported_by is not None \
                and reported_by in mechanism_only_services:
            # 影响层证据只能给**已有的候选服务**补充机制，不能凭空建候选。
            # 判据是"这个服务是否已经是候选"（任何机制都算），
            # 而不是"这个具体机制是否已存在" —— 否则 redis 已知是候选、
            # 但还没人提出 pool_exhaustion 时，兄弟证据就无法补上这个机制，
            # 而那恰恰是它最有价值的地方。
            if not any(k[0] == service for k in seen):
                return
        key = (service, fault_type)
        existing = seen.get(key)
        if existing is not None:
            # 更具体的组件信息优先，但不新增候选
            if component and not existing.candidate_component:
                existing.candidate_component = component
            return
        hypothesis = RootCauseHypothesis(
            investigation_run_id=investigation_run_id,
            candidate_service=service,
            candidate_component=component,
            fault_type=fault_type,
            failure_mechanism=mechanism,
            # 预期观测声明：下传给语义校验（rca/semantic.py），
            # 让"这个假设声称会看到什么"成为可核对的东西，而不是空字段。
            predicted_observations=list(_EXPECTED_BY_FAULT.get(fault_type, ())),
            status=HypothesisStatus.PROPOSED,
        )
        seen[key] = hypothesis
        hypotheses.append(hypothesis)

    for ev in evidence_list:
        if ev.service_id is None:
            continue
        if metric_is_anomalous(ev):
            _add(ev.service_id, "availability_degradation", None,
                 "error rate crossed baseline threshold", reported_by=ev.service_id)
        if logs_are_anomalous(ev):
            text = " ".join(str(r.get("message", "")) for r in ev.normalized_data.get("records", []))
            attribution = _attribute_dependency(text, ev.service_id, topology)
            if attribution is not None:
                owner_service, owner_component = attribution
                fault_type = (
                    "pool_exhaustion" if owner_component and "pool" in owner_component
                    else "dependency_timeout"
                )
                _add(owner_service, fault_type, owner_component,
                     f"error reported by {ev.service_id}: dependency {owner_service} failing",
                     reported_by=ev.service_id)
            else:
                component = _extract_component(text)
                fault_type = (
                    "pool_exhaustion" if component and "pool" in component else "dependency_timeout"
                )
                _add(ev.service_id, fault_type, component,
                     "error logs indicate dependency failure", reported_by=ev.service_id)
        if traces_are_anomalous(ev):
            spans = ev.normalized_data.get("spans", [])
            slowest = max(
                spans,
                key=lambda s: float(s.get("duration_us") or 0),
                default=None,
            )
            span_name = str(slowest.get("operation_name") or "") if slowest else ""
            # span 里的操作名是"调用动作"，不是组件归属于本服务；
            # 依赖名出现在 span 名里时同样归因到被调用的依赖服务。
            attribution = _attribute_dependency(span_name, ev.service_id, topology)
            if attribution is not None:
                owner_service, owner_component = attribution
                _add(owner_service, "latency_degradation", owner_component,
                     f"slow span on {owner_service} observed by {ev.service_id}",
                     reported_by=ev.service_id)
            else:
                # 慢链路但没有可归因的依赖时，**不要**从 span 名切出"组件"。
                # `redis.command` 的第二段是动词（command/get/set），不是组件名。
                # 把动词当组件会让它错误地拿到"组件具体"加分，进而与真实组件
                # （redis-connection-pool）并列，最终只能按 UUID 随机排序。
                _add(ev.service_id, "latency_degradation", None,
                     "slow span on downstream dependency (component unknown)",
                     reported_by=ev.service_id)

    return hypotheses
