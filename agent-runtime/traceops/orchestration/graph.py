"""LangGraph 调查编排（蓝图 §7 Agent Runtime / Investigation Plane）。

节点（保序）: plan → collect → hypothesize → link → rank → report

边界：
- 本层不直接碰数据源，只经 ToolGateway（权限/限额/审计）
- 本层不产出"置信度"，排序只用 rca/ranking.py 的确定性规则
- 预算/截止时间由 BudgetManager 守护；耗尽则 FAILED（不留半成品报告）
"""
from __future__ import annotations

import logging

import functools

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig

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
from traceops.evidence.graph import (
    build_evidence_graph,
    earliest_anomaly,
    ordering_violations,
    promote_relations,
)
from traceops.evidence.templates import QueryTemplateError, default_registry
from traceops.evidence.normalizer import _trace_batches, coerce_epoch, normalize
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.detectors import (
    ERROR_RATE_THRESHOLD,
    attribute_evidence,
    generate_hypotheses,
    logs_are_anomalous,
    metric_is_anomalous,
    traces_are_anomalous,
)
from traceops.rca.chain import build_propagation_chain
from traceops.rca.contradiction import find_contradictions
from traceops.rca.semantic import check_all
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



log = logging.getLogger(__name__)

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
    #: 告警原文（可选）。规则路径不读它，只透传给模型 ——
    #: 这让"能否利用自然语言线索"成为可测的差异来源。
    alert_summary: str | None = None
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
    scope_depth: int = 1  # 向下游依赖展开的层数（调查层）
    # 影响层：向上游（调用方）采集症状证据，只用于传播链/影响范围，不进候选。
    # 关掉它就退化为一层采集（症状不会再出现在链上）。
    impact_scope: bool = True
    # ---- 消融开关（E4 vs E5）----
    # 语义覆盖率闸门：覆盖率为 0 时把 SUPPORTED 降级为 INCONCLUSIVE。
    # 关掉它 = E4 臂（有证据引擎但无假设验证）。
    enable_semantic_gate: bool = True
    # 确定性矛盾检测：生成 CONTRADICTS 关系，供 R-005 降权。
    enable_contradiction: bool = True


@dataclass
class InvestigationResult:
    status: str  # SUPPORTED | REFUTED | INCONCLUSIVE | FAILED
    report: RCAReport | None
    evidence_ids: list[str]
    errors: list[str]
    tool_calls: int
    tokens_used: int


class InvestigationState(TypedDict, total=False):
    """图状态。

    **不包含 RunContext**：它持有 EvidenceStore / adapters / model 等运行时对象，
    不可序列化。放进来会导致 LangGraph checkpoint 失败（msgpack 无法编码），
    也就等于失去崩溃恢复能力。运行时依赖经 config 传递（见 `_ctx_from`）。
    """

    request: InvestigationRequest
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


CTX_CONFIG_KEY = "traceops_ctx"


def _ctx_from(config: Any) -> RunContext:
    """从 config 取运行时依赖。

    依赖不进 state 是有意的：state 会被序列化到 checkpoint，
    塞进不可序列化对象会直接让 checkpoint 失效。
    """
    if config is None:
        raise ValueError("investigation graph requires run context in config")
    configurable = config.get("configurable") if hasattr(config, "get") else None
    ctx = (configurable or {}).get(CTX_CONFIG_KEY)
    if ctx is None:
        raise ValueError(f"config.configurable[{CTX_CONFIG_KEY!r}] is missing")
    return ctx


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


INVESTIGATION_TIER = "investigation"
IMPACT_TIER = "impact"


def _scope_services(request: InvestigationRequest, ctx: "RunContext") -> list[tuple[str, str]]:
    """返回 [(service, tier)]。

    两层语义（见 tests/orchestration/test_two_tier_collection.py 的模块 docstring）：
    - investigation：起点 + 下游依赖 → 参与假设生成与排序
    - impact：上游调用方 → 只参与传播链与影响范围

    不把上游并进 investigation，是因为调用方的错误几乎总是症状；
    让它成为候选等于制造错误根因。
    """
    if ctx.topology is None:
        return [(request.service_hint, INVESTIGATION_TIER)]

    investigation = ctx.topology.expand_scope(request.service_hint, depth=ctx.scope_depth)
    ordered = [request.service_hint] + sorted(investigation - {request.service_hint})
    tiers = [(service, INVESTIGATION_TIER) for service in ordered]

    if ctx.impact_scope:
        upstream = ctx.topology.blast_radius(request.service_hint, depth=ctx.scope_depth)
        # 兄弟节点：与受调查服务共享依赖的旁支服务。它们不是候选根因
        # （investigation 层仍只向下游展开），但它们的异常同样由根因传导而来，
        # 而且它们的日志常常是根因机制最直接的证据。漏掉它们会同时损害
        # 传播链、影响范围与机制判定（F-006 场景实测三项全挂）。
        siblings = ctx.topology.sibling_scope(request.service_hint, depth=ctx.scope_depth)
        extra = sorted(
            (upstream | siblings) - investigation - {request.service_hint}
        )
        tiers.extend((service, IMPACT_TIER) for service in extra)

    return tiers


def _plan_items(request: InvestigationRequest, ctx: "RunContext") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for service, tier in _scope_services(request, ctx):
        base = {
            "service": service,
            "start": request.window_start.isoformat(),
            "end": request.window_end.isoformat(),
        }
        for tool, source_id, evidence_type, template_id in _PLAN_TEMPLATES:
            template_params: dict[str, Any] = {
                "service": service, "start": base["start"], "end": base["end"],
            }
            if tool == "query_service_traces":
                template_params["min_duration_ms"] = SLOW_SPAN_THRESHOLD_MS
            # 模板渲染失败 = 计划非法：宁可少查一个数据源，也不发出未校验的查询
            try:
                rendered_query = default_registry().build(template_id, template_params)
            except QueryTemplateError as exc:
                raise ValueError(
                    f"cannot build query for {service}/{template_id}: {exc}"
                ) from exc

            items.append(
                {
                    "tool": tool,
                    # params 必须严格符合工具 schema；rendered_query 单独携带，只进审计
                    "params": dict(base),
                    "source_id": source_id,
                    "evidence_type": evidence_type,
                    "query_template_id": template_id,
                    "service": service,
                    "tier": tier,
                    "rendered_query": rendered_query,
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
    """从数据本身取观测时间；取不到时用窗口结束时间。

    **必须夹到"现在"**：告警刚触发时查询窗口末端常常在未来（窗口是
    [fault_start, now+X]），于是最后一个数据点的时间戳可能落在未来。
    `Evidence` 有一条不变量 `collected_at >= observed_at`，取值超前会直接
    ValidationError —— 而 `_collect_node` 会把它当成"这个数据源没数据"，
    静默丢进 missing_data 然后 continue。

    这是实测踩到的：跑 MCP 全链路时 metric 和 log 证据全部消失，
    报告照样给出 SUPPORTED，用的是残缺证据。夹到 now 才是诚实的：
    数据确实存在，观测时间不晚于我们取回它的时刻。
    """
    observed = _observed_at_raw(raw, evidence_type, fallback)
    now = datetime.now(UTC)
    return min(observed, now)


def _observed_at_raw(raw: dict[str, Any], evidence_type: EvidenceType, fallback: datetime) -> datetime:
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
                    # 单位自适应：写死 /1e9 会把秒级时间戳解析成 1970
                    return coerce_epoch(values[0][0])
        elif evidence_type is EvidenceType.TRACE:
            # 两种 trace 形状都要认（与 TraceNormalizer 的 `_trace_batches` 同源）：
            #   Jaeger : batches[].spans[].startTime            —— 微秒
            #   OTLP   : *.scopeSpans[].spans[].startTimeUnixNano —— 纳秒
            # 只认 Jaeger 会让所有 OTLP trace 的 observed_at 退化成 fallback（窗口末端），
            # 时间线因此全部相同，传播链的时序标记失去意义。
            for batch in _trace_batches(raw):
                # Jaeger 形状：spans 直接在 batch 下
                for span in batch.get("spans") or []:
                    start_us = span.get("startTime")
                    if start_us is not None:
                        return datetime.fromtimestamp(int(start_us) / 1e6, tz=UTC)
                # OTLP 形状：spans 嵌在 scopeSpans 里，纳秒
                for scope in (batch.get("scopeSpans")
                              or batch.get("instrumentationLibrarySpans") or []):
                    for span in scope.get("spans") or []:
                        start_ns = span.get("startTimeUnixNano")
                        if start_ns is not None:
                            return datetime.fromtimestamp(int(start_ns) / 1e9, tz=UTC)
    except (TypeError, ValueError, IndexError):
        pass
    return fallback


# ---------------- nodes ----------------

def _charged(fn):
    """每个节点执行算一轮，并在入口检查 round / deadline 预算。

    为什么在节点级别计轮：`BudgetManager.begin_round()` 此前**全仓零调用方**，
    于是 `_rounds` 恒为 0（`worker.py` 却把 0 当真实用量上报），
    而 `deadline_seconds` 只在 `begin_round` 里检查 —— 也就从未生效。
    这是"闸门看起来存在、实际不存在"的问题：代码有、测试没测、生产不跑。

    P0 的确定性单趟图确实只有一轮，所以此前无害；但 P1 要接 LLM 多轮循环，
    轮数上限与截止时间就是防止无限烧钱的两道闸门 ——
    带着"闸门是虚的"进 P1 等于没有闸门。
    """
    @functools.wraps(fn)
    def wrapper(state, config):
        ctx = _ctx_from(config)
        try:
            ctx.budget.begin_round()
        except BudgetExhausted as exc:
            # 预算耗尽必须是**干净失败**（返回 FAILED 结果），不是抛出异常。
            #
            # 为什么这一点重要：`run_investigation` 不捕获异常，
            # 所以从节点里抛出 BudgetExhausted 会一路冒到调用方 ——
            # 而 `_collect_node` 内部处理预算时返回的是 FAILED 结果。
            # 两种处理方式不一致，调用方无法统一判断"调查失败了"。
            # 与既有语义对齐：不产出半成品报告，但把原因写进 errors。
            errors = list(state.get("errors", [])) + [f"budget exhausted: {exc}"]
            return {"status": "FAILED", "errors": errors,
                    "missing_data": list(state.get("missing_data", []))}

        return fn(state, config)

    return wrapper


@_charged
def _plan_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    request = state["request"]
    return {"plan": _plan_items(request, _ctx_from(config))}


@_charged
def _collect_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    request = state["request"]
    ctx = _ctx_from(config)
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
                query_params={**item["params"], "_tier": item.get("tier", INVESTIGATION_TIER)},
                # 审计链路：证据必须带上"实际执行了什么查询"
                rendered_query=item.get("rendered_query"),
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


@_charged
def _hypothesize_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    """确定性检测器 + （可选）模型提案。

    模型提案不替代规则：两者合并，进入同一套规则排序。
    模型失败只记 missing_data，不降级整个调查 —— 数据源挂掉都不该终止，
    模型挂掉更不该。
    """
    request = state["request"]
    ctx = _ctx_from(config)
    evidence_list = state.get("evidence", [])
    missing = list(state.get("missing_data", []))

    # 分层规则（三件事分开，别一刀切）：
    #   1. **生成候选**：只有调查层证据可以。影响层是上游/兄弟的症状，
    #      让它们成为候选等于把症状服务当根因。
    #   2. **描述机制**：影响层证据**可以**参与 —— 前提是 `attribute_evidence`
    #      把它归因到某个已有的候选服务。兄弟服务写"redis timeout"的日志，
    #      是关于 redis 的证据，能描述 redis 的机制（F-006 实测：这条被整条
    #      丢掉后，redis 只剩弱信号，机制判成 availability_degradation，
    #      FT@1 直接掉到 0）。
    #   3. **进传播链/影响范围**：影响层证据本来就在做这件事。
    impact_services = {
        e.service_id for e in evidence_list
        if e.query_params.get("_tier", INVESTIGATION_TIER) != INVESTIGATION_TIER
        and e.service_id
    }
    hypotheses = generate_hypotheses(
        evidence_list,
        investigation_run_id=request.investigation_run_id,
        topology=ctx.topology,
        mechanism_only_services=impact_services,
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
            alert_summary=getattr(request, "alert_summary", None),
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


#: 机制关键词：用于给模型"命中了什么"，而不是把日志原文喂进去。
_MECHANISM_KEYWORDS = (
    "pool", "timeout", "timed out", "exhausted", "refused", "unavailable",
    "oom", "memory", "disk", "connection", "rate limit", "circuit",
)


def _evidence_summary(evidence_list: list[Evidence]) -> dict[str, Any]:
    """给模型的证据摘要。

    **必须同时满足两个约束**：

    1. **防注入 + 省 token**：原始日志文本不进 Prompt（日志是外部输入，
       其中可能藏有指令）。这一条不能因为"信息量不够"就放弃。
    2. **信息量足够到能提假设**：只给 `{count, services}` 时，模型知道
       "有 3 条 redis 的日志"但不知道日志说了什么，只能靠 service_hint 猜 ——
       于是"模型假设不如规则"会被误读成模型能力问题，而不是我们的输入问题。

    折中的判据：**归一化后的结构化特征是安全的，自由文本不是**。

    | 给 | 不给 |
    |---|---|
    | 错误率峰值 + 是否超阈值 | 日志原文 |
    | 机制关键词命中（pool/timeout/...） | 完整 payload |
    | span 操作名 + 最大耗时（OTLP 结构化字段） | 大段自由文本 |
    | 计数与服务分布（原有锚点） | |
    """
    summary: dict[str, Any] = {}
    for evidence in evidence_list:
        key = evidence.evidence_type.value + "s"
        entry = summary.setdefault(key, {"count": 0, "services": []})
        entry["count"] += 1
        if evidence.service_id and evidence.service_id not in entry["services"]:
            entry["services"].append(evidence.service_id)
        data = evidence.normalized_data or {}

        if evidence.evidence_type is EvidenceType.METRIC:
            peak = _metric_peak(data)
            if peak is not None:
                entry["peak_value"] = peak
                entry["above_threshold"] = peak >= ERROR_RATE_THRESHOLD
        elif evidence.evidence_type is EvidenceType.LOG:
            hits = _log_keyword_hits(data)
            if hits:
                # 只给命中的关键词，不给句子
                entry.setdefault("keyword_hits", [])
                for hit in hits:
                    if hit not in entry["keyword_hits"]:
                        entry["keyword_hits"].append(hit)
        elif evidence.evidence_type is EvidenceType.TRACE:
            slowest = _slowest_span(data)
            if slowest is not None:
                name, duration_ms = slowest
                if name:
                    entry.setdefault("slowest_operations", [])
                    if name not in entry["slowest_operations"]:
                        entry["slowest_operations"].append(name)
                entry["max_duration_ms"] = max(entry.get("max_duration_ms", 0), duration_ms)
    return summary


def _metric_peak(data: dict[str, Any]) -> float | None:
    """指标序列的最大值 —— 数字是安全的，且模型需要它判断严重程度。"""
    peak: float | None = None
    for item in data.get("series") or []:
        for point in item.get("values") or []:
            try:
                value = float(point[1])
            except (TypeError, ValueError, IndexError):
                continue
            peak = value if peak is None else max(peak, value)
    return peak


def _log_keyword_hits(data: dict[str, Any]) -> list[str]:
    """日志命中的**机制关键词**（不是日志原文）。"""
    hits: list[str] = []
    for record in data.get("records") or []:
        blob = str(record.get("message", "")).lower()
        for keyword in _MECHANISM_KEYWORDS:
            if keyword in blob and keyword not in hits:
                hits.append(keyword)
    return hits


def _slowest_span(data: dict[str, Any]) -> tuple[str, int] | None:
    """最慢 span 的 (操作名, 毫秒)。操作名来自 OTLP 结构化字段，非自由文本。"""
    best: tuple[str, int] | None = None
    for span in data.get("spans") or []:
        try:
            duration_ms = int(float(span.get("duration_us") or 0) / 1000)
        except (TypeError, ValueError):
            continue
        if best is None or duration_ms > best[1]:
            best = (str(span.get("operation_name") or ""), duration_ms)
    return best


@_charged
def _link_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx_from(config)
    request = state["request"]
    links: list[HypothesisEvidence] = []
    for hypothesis in state.get("hypotheses", []):
        for evidence in state.get("evidence", []):
            if evidence.evidence_type is EvidenceType.EVENT:
                component = _event_component(evidence)
                if not component:
                    continue  # 组件不明 → 不给任何假设加 R-004，避免瞎归因
                # 组件匹配分三档（从强到弱），任一成立即算"这次变更解释了该假设"：
                #   1) 组件名完全相等
                #   2) 组件名包含候选服务的名字（redis-connection-pool ⊃ redis）
                #   3) 候选服务包含组件名的前缀（order-service ⊂ order-service-thread-pool）
                # 只比字符串会让 redis 的假设永远拿不到 R-004，
                # 因为检测器推出的组件名是 "redis-connection-pool" 而候选常是 None。
                if _component_matches(component, hypothesis):
                    links.append(
                        HypothesisEvidence(
                            hypothesis_id=hypothesis.id,
                            evidence_id=evidence.id,
                            relation_type=RelationType.SUPPORTS,
                            reason=f"configuration change on {component} preceded fault",
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

    # 反向证据：主动构造 CONTRADICTS 关系。
    # 此前这条链**只产生 SUPPORTS**，于是 R-005（冲突证据降权）成了死代码，
    # Contradiction Detection Rate 的分母恒为 0。矛盾的判据全部可枚举
    # （见 rca/contradiction.py），因此可确定性生成，不需要 LLM。
    hypotheses = list(state.get("hypotheses", []))
    # E4 臂关闭矛盾检测：它测的是"有证据引擎、无假设验证"
    contradicting = (
        find_contradictions(
            hypotheses, list(state.get("evidence", [])), topology=ctx.topology)
        if getattr(ctx, "enable_contradiction", True) else []
    )
    for item in contradicting:
        # **矛盾优先于支持**：同一条证据如果被判为反证，它就不该再算作支持。
        #
        # 为什么不是"跳过重复对"：SUPPORTS 是按"证据归属 == 候选服务"粗判的，
        # 而矛盾判定是**针对性**的（"这个服务的错误率全程正常，所以池耗尽
        # 不成立"）。后者更强，必须覆盖前者，否则 R-005 永远读不到冲突。
        links = [l for l in links
                 if not (l.hypothesis_id == item.hypothesis_id
                         and l.evidence_id == item.evidence_id)]
        links.append(
            HypothesisEvidence(
                hypothesis_id=item.hypothesis_id,
                evidence_id=item.evidence_id,
                relation_type=RelationType.CONTRADICTS,
                reason=item.reason,
                created_by=CreatedBy.RULE,
            )
        )
    return {"links": links}


def _component_matches(component: str, hypothesis: RootCauseHypothesis) -> bool:
    """配置变更组件与候选假设是否指同一对象。"""
    component_lower = component.lower()
    service_lower = (hypothesis.candidate_service or "").lower()
    candidate_component = (hypothesis.candidate_component or "").lower()

    if not component_lower:
        return False
    if candidate_component and component_lower == candidate_component:
        return True
    # 组件名里含服务名：redis-connection-pool 属于 redis
    if service_lower and service_lower in component_lower:
        return True
    # 候选组件名里含服务名，且变更组件以前者开头（order-service-thread-pool）
    if candidate_component and service_lower and service_lower in candidate_component \
            and component_lower == candidate_component:
        return True
    return False


def _event_component(evidence: Evidence) -> str | None:
    """兼容两种归一化形状：{"component": ...} 与 {"event": {"component": ...}}。"""
    for event in evidence.normalized_data.get("events", []):
        component = event.get("component")
        if not component and isinstance(event.get("event"), dict):
            component = event["event"].get("component")
        if component:
            return str(component)
    return None


@_charged
def _correlate_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    """证据关联（蓝图 §6.3）：构建证据图，产出时间线与关联统计。

    不产出"因果结论"：这里只回答"哪些证据属于同一次故障链、谁先谁后"，
    由诊断层负责解释。
    """
    ctx = _ctx_from(config)
    request = state["request"]
    evidence_list = state.get("evidence", [])
    missing = list(state.get("missing_data", []))
    if not evidence_list:
        return {"evidence_graph_summary": {}, "missing_data": missing}

    graph = build_evidence_graph(evidence_list, topology=ctx.topology)
    earliest = earliest_anomaly(evidence_list)
    # 内部边提升为领域关系并持久化：否则关系只活在内存里，重启即丢
    relations = promote_relations(graph)
    for relation in relations:
        ctx.store.add_relation(relation, incident_id=request.incident_id)
    summary: dict[str, Any] = {
        "evidence_nodes": len(graph.evidence),
        "edges": len(graph.edges),
        "edge_rules": sorted({edge.rule for edge in graph.edges}),
        "services": sorted(graph.services()),
        "timeline": graph.timeline(),
        "earliest_anomaly_id": earliest.id if earliest else None,
        "earliest_anomaly_service": earliest.service_id if earliest else None,
        "relations_persisted": len(relations),
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


@_charged
def _rank_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    request = state["request"]
    hypotheses = state.get("hypotheses", [])
    if not hypotheses:
        return {"ranked": []}
    signals = [
        derive_signals(h, state.get("links", []), _ctx_from(config).store, fault_start=request.fault_start)
        for h in hypotheses
    ]
    ranked = rank_hypotheses(hypotheses, signals, topology=_ctx_from(config).topology)

    # 拓扑分层：同分时越靠下游（更可能是根因）的排前。
    # 规则分不同则不动 —— 证据强度优先，拓扑只做平局裁决。
    #
    # 注意：R-008 已把「症状」候选降级（status != SUPPORTED）。这里的重排
    # 必须把状态放在第一优先级，否则会把被降级的症状服务重新顶到 rank1，
    # R-008 就白做了。
    topology = _ctx_from(config).topology
    if topology is not None:
        from traceops.domain.models import HypothesisStatus

        order = {name: index for index, name in enumerate(topology.blame_order({h.candidate_service for h in hypotheses}))}
        by_id = {h.id: h for h in hypotheses}
        ranked.sort(key=lambda r: (
            r.status is not HypothesisStatus.SUPPORTED,   # 断言为根因的排前面
            -r.score,
            order.get(by_id[r.hypothesis_id].candidate_service, 999),
        ))
        for index, candidate in enumerate(ranked, start=1):
            candidate.rank = index
    return {"ranked": ranked}


def _affected_services(state: InvestigationState, config: RunnableConfig) -> list[str]:
    """影响范围 = 首选根因的上游服务中，**确实被观测到异常**的那些。

    为什么不能只算拓扑：`blast_radius` 只说明"这两个服务有调用关系"，
    不说明"这次故障真的波及了它"。拓扑里有依赖 ≠ 它此刻在受苦。

    参考 aurora-lab 场景 12 的失败：「其它 workload 都健康、未受影响」
    的唯一依据是 pod status，从未查过流量 —— 真实第二故障因此被漏掉，
    而报告还宣称它不存在。

    所以这里只收录**采集层拿到了证据**的上游服务；拓扑相关但没采到的，
    由 `_unobserved_related` 记入 missing_data（明确说"没查"，
    而不是默认"没事"）。
    """
    topology = _ctx_from(config).topology
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
    anomalous = _anomalous_services(state)
    return sorted(name for name in radius if name in anomalous)


def _anomalous_services(state: InvestigationState) -> set[str]:
    """本次调查**观测到异常**的服务集合（受影响判定用这个，不是"有数据"）。

    `_observed_services` 只知道"拿到了记录"；影响范围要的是"它确实表现异常"。
    一个服务错误率 0.01（正常）不该出现在受影响列表里，否则报告会夸大影响面。
    """
    names: set[str] = set()
    for evidence in state.get("evidence", []) or []:
        service = evidence.service_id
        if not service:
            continue
        if metric_is_anomalous(evidence) or logs_are_anomalous(evidence) or traces_are_anomalous(evidence):
            names.add(service)
    return names


def _observed_services(state: InvestigationState) -> set[str]:
    """本次调查真正**采到有效观测**的服务集合。

    "查过"不等于"观测到"：一次查询可能成功返回但结果为空（`result: []`），
    那种情况下我们什么都没看到。把这种服务算成"已观测"，等于用
    "我查了"冒充"我看见它没事"——正是 aurora-lab E-1 的失败模式。

    因此只统计 `normalized_data` 里确实有记录的证据。
    """
    names: set[str] = set()
    for evidence in state.get("evidence", []) or []:
        service = evidence.service_id
        if not service:
            continue
        data = evidence.normalized_data or {}
        records = (
            data.get("total_records")
            or data.get("result_count")
            or len(data.get("records") or [])
            or len(data.get("spans") or [])
            or len(data.get("series") or [])
        )
        if records:
            names.add(service)
    return names


def _services_outside_investigation(state: InvestigationState, config: RunnableConfig) -> list[str]:
    """拓扑中存在、但本次调查完全没碰过的服务。

    这是"覆盖边界"而非"故障"：从单一告警起点出发不可能知道别的链有没有问题，
    所以只能诚实声明边界，不能假装全都查过。
    """
    topology = _ctx_from(config).topology
    request = state["request"]
    if topology is None:
        return []
    depth = _ctx_from(config).scope_depth
    covered = (
        topology.expand_scope(request.service_hint, depth=depth)
        | topology.blast_radius(request.service_hint, depth=depth)
        | topology.sibling_scope(request.service_hint, depth=depth)
    )
    return sorted(topology.services() - covered)


def _unobserved_related(state: InvestigationState, config: RunnableConfig) -> list[str]:
    """拓扑相关但**没有观测证据**的服务 —— 必须显式记为"未检查"。

    这是"诚实"的另一半：不能把没查的服务写进影响范围（那是发明事实），
    但也不能静默省略（静默省略等于暗示"查过了、没事"）。
    """
    topology = _ctx_from(config).topology
    ranked = state.get("ranked") or []
    hypotheses = state.get("hypotheses") or []
    if topology is None or not ranked:
        return []
    lead = next((h for h in hypotheses if h.id == ranked[0].hypothesis_id), None)
    if lead is None:
        return []
    radius = topology.blast_radius(lead.candidate_service, depth=2)
    radius.discard(lead.candidate_service)
    observed = _observed_services(state)
    return sorted(name for name in radius if name not in observed)


def _propagation_chain(state: InvestigationState, ctx: RunContext) -> list:
    """按首选根因构建传播链（含无独立故障机制但受影响的节点）。"""
    ranked = state.get("ranked") or []
    hypotheses = state.get("hypotheses") or []
    evidence_list = state.get("evidence") or []
    if not ranked:
        return []
    top_id = ranked[0].hypothesis_id
    lead = next((h for h in hypotheses if h.id == top_id), None)
    if lead is None:
        return []
    return build_propagation_chain(
        root_service=lead.candidate_service,
        evidence_list=evidence_list,
        topology=ctx.topology,
    )


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


@_charged
def _report_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    request = state["request"]
    ctx = _ctx_from(config)
    missing = list(state.get("missing_data", []))
    # 拓扑相关但没观测到数据的服务必须在报告里明说"未检查"——
    # 既不写进影响范围（那是发明事实），也不静默省略（那暗示"查过了没事"）。
    unobserved = _unobserved_related(state, config)
    if unobserved:
        missing.append(
            "topology-related services not observed (scope unknown, not assumed healthy): "
            + ", ".join(unobserved)
        )
    # 覆盖边界必须**主动**声明。调查从一条告警的 service_hint 出发，
    # 只能覆盖该点所在的链路；拓扑里其它独立的链根本没被查。
    # 不声明的话，读者会以为"报告没说它们有问题 = 它们没问题"，
    # 这正是 aurora-lab 场景 12 的失败（第二条故障从未出现，而报告不说"我没查"）。
    outside = _services_outside_investigation(state, config)
    if outside:
        missing.append(
            "services outside the investigated chain were NOT investigated "
            "(this report covers one alert's chain only): " + ", ".join(outside)
        )
    report = build_report(
        investigation_run_id=request.investigation_run_id,
        hypotheses=state.get("hypotheses", []),
        links=state.get("links", []),
        evidence=state.get("evidence", []),
        ranked=state.get("ranked", []),
        fault_start=request.fault_start,
        store=ctx.store,
        missing_data=missing,
        affected_services=_affected_services(state, config),
        correlation_notes=_correlation_notes(state),
        propagation_chain=_propagation_chain(state, ctx),
        incident_id=request.incident_id,
        topology=ctx.topology,
        semantic_checks=(
            {
                check.hypothesis_id: check
                for check in check_all(
                    list(state.get("hypotheses", [])),
                    list(state.get("evidence", [])),
                    topology=ctx.topology,
                )
            }
            if ctx.enable_semantic_gate else None
        ),
    )
    return {"report": report, "status": report.diagnosis_status}


def _build_gateway(ctx: RunContext) -> ToolGateway:
    gateway = ToolGateway()
    register_builtin_tools(gateway, ctx.adapters)
    return gateway


def _route_after_collect(state: InvestigationState) -> str:
    return "stop" if state.get("status") == "FAILED" else "continue"


def build_investigation_graph(*, checkpointer: Any | None = None,
                              interrupt_before: list[str] | None = None):
    """构建调查图。

    Args:
        checkpointer: LangGraph checkpointer；提供时支持中断/恢复（P2）。
        interrupt_before: 在这些节点前中断（用于模拟崩溃点、验证恢复语义）。
    """
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
    return builder.compile(checkpointer=checkpointer, interrupt_before=interrupt_before or [])


def _initial_state(request: InvestigationRequest) -> InvestigationState:
    return {
        "request": request,
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


def _run_with_progress(graph, initial_state, config, on_node):
    """按节点执行并回调 `on_node(node_name, state)`，返回最终合并 state。

    LangGraph 的 `stream()` 以 `(node_name, update)` 形式产出增量；
    这里把增量合并回完整 state，这样调用方拿到的 final 与 invoke() 一致。
    """
    accumulated: dict[str, Any] = dict(initial_state)
    for chunk in graph.stream(initial_state, config, stream_mode="updates"):
        if not isinstance(chunk, dict):
            continue
        for node_name, update in chunk.items():
            if isinstance(update, dict):
                accumulated.update(update)
            on_node(node_name, accumulated)
    return accumulated


def run_investigation(request: InvestigationRequest, ctx: RunContext, *,
                      thread_id: str | None = None,
                      checkpointer: Any | None = None,
                      on_node=None) -> InvestigationResult:
    """执行一次完整调查。异常只允许在编程错误时抛出；业务失败进 result。

    Args:
        thread_id: 提供时用作 checkpoint 的线程标识（同一 thread 的多次调用共享
            进度，用于崩溃恢复）。
        checkpointer: 提供时启用 checkpointing。
    """
    if ctx.gateway is None:
        ctx.gateway = _build_gateway(ctx)
    graph = build_investigation_graph(checkpointer=checkpointer)
    configurable: dict[str, Any] = {CTX_CONFIG_KEY: ctx}
    if thread_id:
        configurable["thread_id"] = thread_id
    config = {"configurable": configurable}
    if on_node is None:
        final = graph.invoke(_initial_state(request), config)
    else:
        # stream() 按节点产出 state 更新；用它把"走到哪一步"实时交出去。
        # 不用 invoke 的另一种原因：进度必须在**节点完成时**发，
        # 事后一次性发全部进度等于放录像，SSE 就没意义了。
        final = _run_with_progress(graph, _initial_state(request), config, on_node)
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
