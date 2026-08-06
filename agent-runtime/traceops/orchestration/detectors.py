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
) -> list[RootCauseHypothesis]:
    """从证据生成候选假设（确定性）。同一 (service, fault_type, component) 只生成一次。"""
    seen: set[tuple[str, str, str | None]] = set()
    hypotheses: list[RootCauseHypothesis] = []

    def _add(service: str, fault_type: str, component: str | None, mechanism: str) -> None:
        key = (service, fault_type, component)
        if key in seen:
            return
        seen.add(key)
        hypotheses.append(
            RootCauseHypothesis(
                investigation_run_id=investigation_run_id,
                candidate_service=service,
                candidate_component=component,
                fault_type=fault_type,
                failure_mechanism=mechanism,
                predicted_observations=[],
                status=HypothesisStatus.PROPOSED,
            )
        )

    for ev in evidence_list:
        if ev.service_id is None:
            continue
        if metric_is_anomalous(ev):
            _add(ev.service_id, "availability_degradation", None, "error rate crossed baseline threshold")
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
                     f"error reported by {ev.service_id}: dependency {owner_service} failing")
            else:
                component = _extract_component(text)
                fault_type = (
                    "pool_exhaustion" if component and "pool" in component else "dependency_timeout"
                )
                _add(ev.service_id, fault_type, component, "error logs indicate dependency failure")
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
                     f"slow span on {owner_service} observed by {ev.service_id}")
            else:
                prefix = span_name.split(".")[1] if "." in span_name else ""
                component = prefix or None
                _add(ev.service_id, "latency_degradation", component,
                     "slow span on downstream dependency")

    return hypotheses
