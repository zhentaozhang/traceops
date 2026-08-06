"""LangGraph 调查编排（蓝图 §7 Agent Runtime / Investigation Plane）。

节点（保序）: plan → collect → hypothesize → link → rank → report

边界：
- 本层不直接碰数据源，只经 ToolGateway（权限/限额/审计）
- 本层不产出"置信度"，排序只用 rca/ranking.py 的确定性规则
- 预算/截止时间由 BudgetManager 守护；耗尽则 FAILED（不留半成品报告）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, model_validator

from traceops.domain.models import (
    CreatedBy,
    Evidence,
    EvidenceType,
    HypothesisEvidence,
    RCAReport,
    RelationType,
    RootCauseHypothesis,
)
from traceops.evidence.graph import build_evidence_graph, earliest_anomaly, ordering_violations
from traceops.evidence.normalizer import normalize
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.detectors import attribute_evidence, generate_hypotheses
from traceops.rca.ranking import RankedCandidate, derive_signals, rank_hypotheses
from traceops.rca.report import build_report
from traceops.runtime.budget import BudgetExhausted, BudgetManager
from traceops.runtime.model import ModelGateway, ModelRequest, ModelUnavailable, hypothesis_to_domain
from traceops.tools.gateway import ToolGateway, register_builtin_tools

SLOW_SPAN_THRESHOLD_MS = 500

# (tool_name, source_id, evidence_type, query_template_id)
_PLAN_TEMPLATES: list[tuple[str, str, EvidenceType, str]] = [
    ("query_service_error_rate", "prometheus", EvidenceType.METRIC, "qt-error-rate-v1"),
    ("query_service_logs", "loki", EvidenceType.LOG, "qt-error-logs-v1"),
    ("query_service_traces", "tempo", EvidenceType.TRACE, "qt-slow-traces-v1"),
]


class InvestigationRequest(BaseModel):
    """一次调查的输入。对应 contracts/events/v1/investigation.requested.schema.json。"""

    model_config = ConfigDict(frozen=True)

    investigation_run_id: str
    incident_id: str
    environment: str = "demo"
    service_hint: str
    fault_start: datetime
    window_start: datetime
    window_end: datetime
    config_change: dict[str, Any] | None = None
    priority: str = "P2"

    @model_validator(mode="after")
    def _validate_times(self) -> InvestigationRequest:
        for name, value in (
            ("fault_start", self.fault_start),
            ("window_start", self.window_start),
            ("window_end", self.window_end),
        ):
            if value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware (UTC)")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be before window_end")
        if not (self.window_start <= self.fault_start <= self.window_end):
            raise ValueError("fault_start must fall inside the investigation window")
        if self.config_change is not None:
            changed_at = self.config_change.get("changed_at")
            if not changed_at:
                raise ValueError("config_change requires changed_at")
            _parse_dt(changed_at)  # validate parseable
        return self


@dataclass
class RunContext:
    """一次调查的运行时依赖（不可序列化对象，仅存在于进程内）。"""

    store: EvidenceStore
    adapters: Any
    budget: BudgetManager = field(default_factory=BudgetManager)
    gateway: ToolGateway | None = None
    model: Any | None = None  # ModelClient；None 时只用确定性检测器
    topology: Any | None = None  # ServiceTopology；None 时退化为单服务调查
    scope_depth: int = 1  # 向下游依赖展开的层数


@dataclass
class InvestigationResult:
    status: str  # SUPPORTED | REFUTED | INCONCLUSIVE | FAILED
    report: RCAReport | None
    evidence_ids: list[str]
    errors: list[str]
    tool_calls: int
    tokens_used: int


class InvestigationState(TypedDict, total=False):
    request: InvestigationRequest
    ctx: RunContext
    plan: list[dict[str, Any]]
    evidence: list[Evidence]
    hypotheses: list[RootCauseHypothesis]
    links: list[HypothesisEvidence]
    ranked: list[RankedCandidate]
    evidence_graph_summary: dict[str, Any]
    report: RCAReport | None
    status: str
    errors: list[str]
    missing_data: list[str]


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _scope_services(request: InvestigationRequest, ctx: "RunContext") -> list[str]:
    """调查范围：service_hint + 其下游依赖（按拓扑展开）。

    不向上游展开：调用方看到的是症状，不是根因所在。
    """
    if ctx.topology is None:
        return [request.service_hint]
    scope = ctx.topology.expand_scope(request.service_hint, depth=ctx.scope_depth)
    services = sorted(scope)
    # service_hint 排在最前，便于阅读审计记录
    services.remove(request.service_hint)
    return [request.service_hint] + services


def _plan_items(request: InvestigationRequest, ctx: "RunContext") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for service in _scope_services(request, ctx):
        base = {
            "service": service,
            "start": request.window_start.isoformat(),
            "end": request.window_end.isoformat(),
        }
        for tool, source_id, evidence_type, template_id in _PLAN_TEMPLATES:
            params = dict(base)
            if tool == "query_service_traces":
                params["min_duration_ms"] = SLOW_SPAN_THRESHOLD_MS
            items.append(
                {
                    "tool": tool,
                    "params": params,
                    "source_id": source_id,
                    "evidence_type": evidence_type,
                    "query_template_id": template_id,
                    "service": service,
                }
            )
    if request.config_change is not None:
        changed_at = _parse_dt(request.config_change["changed_at"])
        items.append(
            {
                "tool": "config_change_event",
                "params": {},
                "source_id": "control-plane",
                "evidence_type": EvidenceType.EVENT,
                "query_template_id": "qt-config-change-v1",
                "payload": dict(request.config_change),
                "observed_at": changed_at,
            }
        )
    return items


def _unwrap_raw(raw: Any, evidence_type: EvidenceType) -> dict[str, Any]:
    """数据源原生响应 → Normalizer 期待的内层形状。"""
    if not isinstance(raw, dict):
        raise ValueError(f"unexpected datasource payload type: {type(raw).__name__}")
    if evidence_type is EvidenceType.METRIC:
        # Prometheus: {"status": "success", "data": {"resultType", "result"}} → {"resultType", "result"}
        if "data" in raw and isinstance(raw["data"], dict):
            return raw["data"]
    if evidence_type is EvidenceType.LOG:
        # Loki: {"status": "success", "data": {"resultType": "streams", "result": [...]}}
        #   → {"streams": [...]}（LogNormalizer 的输入形状）
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        if "streams" in data:
            return data
        if "result" in data:
            return {"streams": data["result"], "resultType": data.get("resultType", "streams")}
    return raw


def _observed_at(raw: dict[str, Any], evidence_type: EvidenceType, fallback: datetime) -> datetime:
    """从数据本身取观测时间；取不到时用窗口结束时间。"""
    try:
        if evidence_type is EvidenceType.METRIC:
            result = raw.get("result") or []
            if result:
                values = result[0].get("values") or []
                if values:
                    return datetime.fromtimestamp(float(values[-1][0]), tz=UTC)
        elif evidence_type is EvidenceType.LOG:
            streams = raw.get("streams") or []
            for stream in streams:
                values = stream.get("values") or []
                if values:
                    return datetime.fromtimestamp(int(values[0][0]) / 1e9, tz=UTC)
        elif evidence_type is EvidenceType.TRACE:
            for batch in raw.get("batches") or []:
                for span in batch.get("spans") or []:
                    start_us = span.get("startTime")
                    if start_us is not None:
                        return datetime.fromtimestamp(int(start_us) / 1e6, tz=UTC)
    except (TypeError, ValueError, IndexError):
        pass
    return fallback


# ---------------- nodes ----------------

def _plan_node(state: InvestigationState) -> dict[str, Any]:
    request = state["request"]
    return {"plan": _plan_items(request, state["ctx"])}


def _collect_node(state: InvestigationState) -> dict[str, Any]:
    request = state["request"]
    ctx = state["ctx"]
    gateway = ctx.gateway or _build_gateway(ctx)
    ctx.gateway = gateway
    evidence_list: list[Evidence] = []
    missing = list(state.get("missing_data", []))

    for item in state["plan"]:
        # 内置工具走 Gateway（权限/限额/审计）；事件证据由控制面事件转换而来，不调工具
        if item["tool"] == "config_change_event":
            raw = {"event": {**item["payload"], "type": "config_change"}}
            observed_at = item["observed_at"]
            window = (observed_at, observed_at)
        else:
            try:
                raw = gateway.invoke(item["tool"], item["params"], budget=ctx.budget)
            except BudgetExhausted as exc:
                errors = list(state.get("errors", [])) + [f"budget exhausted: {exc}"]
                return {"status": "FAILED", "errors": errors, "missing_data": missing}
            except Exception as exc:  # 单数据源失败不终止调查，记入 missing_data
                missing.append(f"{item['tool']}: {exc}")
                continue
            unwrapped = _unwrap_raw(raw, item["evidence_type"])
            observed_at = _observed_at(unwrapped, item["evidence_type"], request.window_end)
            window = (request.window_start, request.window_end)
            raw = unwrapped

        try:
            evidence = normalize(
                evidence_type=item["evidence_type"],
                source_id=item["source_id"],
                incident_id=request.incident_id,
                run_id=request.investigation_run_id,
                service_id=(
                    item.get("service", request.service_hint)
                    if item["evidence_type"] is not EvidenceType.EVENT
                    else None
                ),
                query_template_id=item["query_template_id"],
                query_params=item["params"],
                raw_payload=raw,
                observed_at=observed_at,
                window=window,
            )
        except Exception as exc:  # 空结果/形状异常 → 记为缺失数据，不伪造证据
            missing.append(f"{item['tool']}: {exc}")
            continue

        ctx.store.register_source(item["source_id"])
        ctx.store.add(evidence)
        evidence_list.append(evidence)

    return {"evidence": evidence_list, "missing_data": missing}


def _hypothesize_node(state: InvestigationState) -> dict[str, Any]:
    """确定性检测器 + （可选）模型提案。

    模型提案不替代规则：两者合并，进入同一套规则排序。
    模型失败只记 missing_data，不降级整个调查 —— 数据源挂掉都不该终止，
    模型挂掉更不该。
    """
    request = state["request"]
    ctx = state["ctx"]
    evidence_list = state.get("evidence", [])
    missing = list(state.get("missing_data", []))

    hypotheses = generate_hypotheses(
        evidence_list,
        investigation_run_id=request.investigation_run_id,
        topology=ctx.topology,
    )

    if ctx.model is not None and evidence_list:
        allowed = sorted({e.service_id for e in evidence_list if e.service_id})
        if ctx.topology is not None:
            allowed = sorted(set(allowed) | ctx.topology.services())
        model_request = ModelRequest(
            incident_id=request.incident_id,
            service_hint=request.service_hint,
            evidence_summary=_evidence_summary(evidence_list),
            allowed_services=allowed,
        )
        gateway = ModelGateway(ctx.model, ctx.budget)
        try:
            response = gateway.propose(model_request)
        except BudgetExhausted as exc:
            # 模型预算耗尽：保留已有规则假设，不终止调查
            missing.append(f"model budget exhausted: {exc}")
            response = None
        except ModelUnavailable as exc:
            missing.append(f"model unavailable: {exc}")
            response = None
        if response is not None:
            existing = {(h.candidate_service, h.fault_type, h.candidate_component) for h in hypotheses}
            for proposal in response.hypotheses:
                key = (proposal.candidate_service, proposal.fault_type, proposal.candidate_component)
                if key in existing:
                    continue  # 与规则假设重复，不产生并列候选
                existing.add(key)
                hypotheses.append(hypothesis_to_domain(
                    proposal, investigation_run_id=request.investigation_run_id))

    if not hypotheses:
        missing.append("no candidate hypotheses generated from collected evidence")
    return {"hypotheses": hypotheses, "missing_data": missing}


def _evidence_summary(evidence_list: list[Evidence]) -> dict[str, Any]:
    """给模型的证据摘要：只给类型/服务/计数，不给原始负载（上下文成本 + 防注入）。"""
    summary: dict[str, Any] = {}
    for evidence in evidence_list:
        key = evidence.evidence_type.value + "s"
        entry = summary.setdefault(key, {"count": 0, "services": []})
        entry["count"] += 1
        if evidence.service_id and evidence.service_id not in entry["services"]:
            entry["services"].append(evidence.service_id)
    return summary


def _link_node(state: InvestigationState) -> dict[str, Any]:
    ctx = state["ctx"]
    request = state["request"]
    links: list[HypothesisEvidence] = []
    for hypothesis in state.get("hypotheses", []):
        for evidence in state.get("evidence", []):
            if evidence.evidence_type is EvidenceType.EVENT:
                component = _event_component(evidence)
                # 配置变更只支持 "候选组件与之匹配" 的假设；组件不明则不给 R-004 加分
                if component and hypothesis.candidate_component and (
                    component.lower() == hypothesis.candidate_component.lower()
                ):
                    links.append(
                        HypothesisEvidence(
                            hypothesis_id=hypothesis.id,
                            evidence_id=evidence.id,
                            relation_type=RelationType.SUPPORTS,
                            reason=f"configuration change on {component or 'candidate'} preceded fault",
                            created_by=CreatedBy.RULE,
                        )
                    )
                continue
            # 证据归属优先：order-service 的日志讲 redis，就是关于 redis 的证据
            attribution = attribute_evidence(evidence, ctx.topology)
            owner = attribution[0] if attribution else evidence.service_id
            if owner == hypothesis.candidate_service:
                links.append(
                    HypothesisEvidence(
                        hypothesis_id=hypothesis.id,
                        evidence_id=evidence.id,
                        relation_type=RelationType.SUPPORTS,
                        reason=(
                            f"{evidence.evidence_type.value} evidence about {owner} "
                            f"(reported by {evidence.service_id})"
                        ),
                        created_by=CreatedBy.RULE,
                    )
                )
    # 交叉检查：事件证据的服务（若有）不属于候选服务的假设不获得 R-004 支持
    return {"links": links}


def _event_component(evidence: Evidence) -> str | None:
    """兼容两种归一化形状：{"component": ...} 与 {"event": {"component": ...}}。"""
    for event in evidence.normalized_data.get("events", []):
        component = event.get("component")
        if not component and isinstance(event.get("event"), dict):
            component = event["event"].get("component")
        if component:
            return str(component)
    return None


def _correlate_node(state: InvestigationState) -> dict[str, Any]:
    """证据关联（蓝图 §6.3）：构建证据图，产出时间线与关联统计。

    不产出"因果结论"：这里只回答"哪些证据属于同一次故障链、谁先谁后"，
    由诊断层负责解释。
    """
    ctx = state["ctx"]
    request = state["request"]
    evidence_list = state.get("evidence", [])
    missing = list(state.get("missing_data", []))
    if not evidence_list:
        return {"evidence_graph_summary": {}, "missing_data": missing}

    graph = build_evidence_graph(evidence_list, topology=ctx.topology)
    earliest = earliest_anomaly(evidence_list)
    summary: dict[str, Any] = {
        "evidence_nodes": len(graph.evidence),
        "edges": len(graph.edges),
        "edge_rules": sorted({edge.rule for edge in graph.edges}),
        "services": sorted(graph.services()),
        "timeline": graph.timeline(),
        "earliest_anomaly_id": earliest.id if earliest else None,
        "earliest_anomaly_service": earliest.service_id if earliest else None,
    }

    # 时间线污染检测：若已知拓扑顺序，检查症状是否早于根因
    if ctx.topology is not None and earliest is not None and earliest.service_id:
        order = ctx.topology.blame_order(graph.services())
        violations = ordering_violations(evidence_list, expected_order=order)
        if violations:
            summary["timeline_violations"] = violations
            missing.append(
                "timeline ordering suspicious: " + "; ".join(violations)
            )
    return {"evidence_graph_summary": summary, "missing_data": missing}


def _rank_node(state: InvestigationState) -> dict[str, Any]:
    request = state["request"]
    hypotheses = state.get("hypotheses", [])
    if not hypotheses:
        return {"ranked": []}
    signals = [
        derive_signals(h, state.get("links", []), state["ctx"].store, fault_start=request.fault_start)
        for h in hypotheses
    ]
    ranked = rank_hypotheses(hypotheses, signals)

    # 拓扑分层：同分时越靠下游（更可能是根因）的排前。
    # 规则分不同则不动 —— 证据强度优先，拓扑只做平局裁决。
    topology = state["ctx"].topology
    if topology is not None:
        order = {name: index for index, name in enumerate(topology.blame_order({h.candidate_service for h in hypotheses}))}
        by_id = {h.id: h for h in hypotheses}
        ranked.sort(key=lambda r: (-r.score, order.get(by_id[r.hypothesis_id].candidate_service, 999)))
        for index, candidate in enumerate(ranked, start=1):
            candidate.rank = index
    return {"ranked": ranked}


def _affected_services(state: InvestigationState) -> list[str]:
    """影响范围 = 首选根因服务的上游（blast radius），排除根因自身。"""
    topology = state["ctx"].topology
    ranked = state.get("ranked") or []
    hypotheses = state.get("hypotheses") or []
    if topology is None or not ranked:
        return []
    top_id = ranked[0].hypothesis_id
    lead = next((h for h in hypotheses if h.id == top_id), None)
    if lead is None:
        return []
    radius = topology.blast_radius(lead.candidate_service, depth=2)
    radius.discard(lead.candidate_service)
    return sorted(radius)


def _correlation_notes(state: InvestigationState) -> list[str]:
    """把关联结论写进报告的 missing_data 之外——用 summary 补充可解释信息。"""
    summary = state.get("evidence_graph_summary") or {}
    notes: list[str] = []
    if summary.get("edges"):
        notes.append(
            f"evidence graph: {summary['evidence_nodes']} nodes / {summary['edges']} edges "
            f"via {', '.join(summary.get('edge_rules', []))}"
        )
    if summary.get("earliest_anomaly_service"):
        notes.append(f"earliest anomaly observed in {summary['earliest_anomaly_service']}")
    return notes


def _report_node(state: InvestigationState) -> dict[str, Any]:
    request = state["request"]
    ctx = state["ctx"]
    report = build_report(
        investigation_run_id=request.investigation_run_id,
        hypotheses=state.get("hypotheses", []),
        links=state.get("links", []),
        evidence=state.get("evidence", []),
        ranked=state.get("ranked", []),
        fault_start=request.fault_start,
        store=ctx.store,
        missing_data=list(state.get("missing_data", [])),
        affected_services=_affected_services(state),
        correlation_notes=_correlation_notes(state),
    )
    return {"report": report, "status": report.diagnosis_status}


def _build_gateway(ctx: RunContext) -> ToolGateway:
    gateway = ToolGateway()
    register_builtin_tools(gateway, ctx.adapters)
    return gateway


def _route_after_collect(state: InvestigationState) -> str:
    return "stop" if state.get("status") == "FAILED" else "continue"


def build_investigation_graph():
    builder = StateGraph(InvestigationState)
    builder.add_node("plan", _plan_node)
    builder.add_node("collect", _collect_node)
    builder.add_node("hypothesize", _hypothesize_node)
    builder.add_node("link", _link_node)
    builder.add_node("correlate", _correlate_node)
    builder.add_node("rank", _rank_node)
    builder.add_node("report", _report_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "collect")
    builder.add_conditional_edges(
        "collect", _route_after_collect, {"stop": END, "continue": "hypothesize"}
    )
    builder.add_edge("hypothesize", "link")
    builder.add_edge("link", "correlate")
    builder.add_edge("correlate", "rank")
    builder.add_edge("rank", "report")
    builder.add_edge("report", END)
    return builder.compile()


def run_investigation(request: InvestigationRequest, ctx: RunContext) -> InvestigationResult:
    """执行一次完整调查。异常只允许在编程错误时抛出；业务失败进 result。"""
    if ctx.gateway is None:
        ctx.gateway = _build_gateway(ctx)
    graph = build_investigation_graph()
    initial: InvestigationState = {
        "request": request,
        "ctx": ctx,
        "plan": [],
        "evidence": [],
        "hypotheses": [],
        "links": [],
        "ranked": [],
        "evidence_graph_summary": {},
        "report": None,
        "status": "RUNNING",
        "errors": [],
        "missing_data": [],
    }
    final = graph.invoke(initial)
    report = final.get("report")
    status = final.get("status", "FAILED")
    if report is None and status != "FAILED":
        status = "FAILED"
    return InvestigationResult(
        status=status,
        report=report,
        evidence_ids=[e.id for e in final.get("evidence", [])],
        errors=list(final.get("errors", [])),
        tool_calls=ctx.budget.tool_calls,
        tokens_used=ctx.budget.tokens_used,
    )
