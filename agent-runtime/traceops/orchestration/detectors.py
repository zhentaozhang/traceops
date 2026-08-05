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


def _extract_component(message: str) -> str | None:
    lowered = message.lower()
    if "redis" in lowered and "pool" in lowered:
        return "redis-connection-pool"
    if "redis" in lowered:
        return "redis"
    match = re.search(r"\b([a-z][a-z0-9-]+-connection-pool)\b", lowered)
    if match:
        return match.group(1)
    return None


def generate_hypotheses(
    evidence_list: list[Evidence], *, investigation_run_id: str
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
            component = _extract_component(text)
            fault_type = "pool_exhaustion" if component and "pool" in component else "dependency_timeout"
            _add(ev.service_id, fault_type, component, "error logs indicate dependency failure")
        if traces_are_anomalous(ev):
            spans = ev.normalized_data.get("spans", [])
            slowest = max(
                spans,
                key=lambda s: float(s.get("duration_us") or 0),
                default=None,
            )
            span_name = str(slowest.get("operation_name") or "") if slowest else ""
            component = span_name.split(".")[0] if "." in span_name else None
            _add(ev.service_id, "latency_degradation", component, "slow span on downstream dependency")

    return hypotheses
