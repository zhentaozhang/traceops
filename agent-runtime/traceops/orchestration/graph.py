"""LangGraph 调查编排（蓝图 §7 Agent Runtime / Investigation Plane）。

节点（保序）: plan → collect → hypothesize → link → rank → report

边界：
- 本层不直接碰数据源，只经 ToolGateway（权限/限额/审计）
- 本层不产出"置信度"，排序只用 rca/ranking.py 的确定性规则
- 预算/截止时间由 BudgetManager 守护；耗尽则 FAILED（不留半成品报告）
"""
from __future__ import annotations

import json
import sqlite3

import logging

import functools

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict

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
from traceops.evidence.templates import (
    QueryTemplateError,
    TelemetryConventions,
    default_registry,
    render_error_rate,
)
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
from traceops.rca.contradiction import find_contradictions, relation_type_of
from traceops.rca.semantic import check_all
from traceops.rca.ranking import RankedCandidate, derive_signals, rank_hypotheses
from traceops.rca.report import build_report
from traceops.topology.discovery import diff_topology, discover_edges
from traceops.runtime.budget import BudgetExhausted, BudgetManager
from traceops.runtime.model import (
    REJECTION_REASONS,
    ModelGateway,
    ModelRequest,
    ModelUnavailable,
    hypothesis_to_domain,
)
from traceops.tools.gateway import SPILL_REF_KEY, ToolGateway, register_builtin_tools

SLOW_SPAN_THRESHOLD_MS = 500

# (tool_name, source_id, evidence_type, query_template_id)
_PLAN_TEMPLATES: list[tuple[str, str, EvidenceType, str]] = [
    ("query_service_error_rate", "prometheus", EvidenceType.METRIC, "qt-error-rate-v1"),
    ("query_service_logs", "loki", EvidenceType.LOG, "qt-error-logs-v1"),
    ("query_service_traces", "tempo", EvidenceType.TRACE, "qt-slow-traces-v1"),
]

#: 判别阶段才查的数据源。放最前面会把每次调查都变贵，放这里只有
#: "两个候选分不开"时才付这笔钱 —— 这正是它该被付的时候。
_DEEPEN_TEMPLATES: list[tuple[str, str, EvidenceType, str]] = [
    ("query_service_profile", "pyroscope", EvidenceType.PROFILE, "qt-profile-v1"),
]

#: 判别回路最多再取证几趟。两趟的理由：一趟分不开两类候选之后，
#: 第二趟能拿到的只是同类数据的另一个切面，再问下去是在赌运气。
DISCRIMINATION_ROUNDS = 2

#: 判别轮的**聚焦窗口**半径（秒）：以最早异常为中心，前后各这么多。
#:
#: 为什么这件事值得再花一次调用：首轮用的是整个故障窗口，几十分钟的
#: 正常波动和真正的起变点混在一起，两个候选看起来一样坏。把窗口收到
#: 起变点附近，"谁先开始坏"才有分辨率 —— 而"谁先坏"是 R-009 用的判据。
#: 换句话说，这一轮买的不是**更多**证据，是**更细的时间分辨率**。
FOCUSED_WINDOW_SECONDS = 60



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
    #: 窗口附近的变更流水（部署 / 配置 / 开关 / 扩缩容）。
    #:
    #: 与 `config_change` 的区别：那个是**一条**"与本次故障相关的变更"，
    #: 由调查发起方挑出来；这里是一段原始变更记录，由调查自己判断哪一条
    #: 在时间上可能与故障有关。RCA 的常识是多数故障由变更引起，
    #: 而只有一个可选字段的变更是过窄的输入。
    changes: list[dict[str, Any]] | None = None
    #: 现在只有契约里的三档。这里曾经是 "P2" —— 契约写 low|normal|high、
    #: 这里写 P2、Java 侧写过 manual，三套词汇互不相识，而运行时谁也没读它。
    #: 一个没人读的字段用哪套词都不报错，所以这三套词并存了很久。
    priority: Literal["low", "normal", "high"] = "normal"
    #: 控制面下发的执行预算（契约 payload.budget）。此前 worker 从不读它，
    #: 契约里的 budget 与 Java 侧硬编码的 50/10/100000/600 一起变成了摆设。
    budget: dict[str, Any] | None = None
    #: 沿下游依赖展开的层数（契约 payload.scope_depth）。
    scope_depth: int | None = None
    #: 本次调查可用的依赖边 [[caller, callee], ...]。
    topology_edges: list[list[str]] | None = None
    #: 这一轮在重做哪一轮（契约 payload.retry_of）。首次尝试为 None。
    #:
    #: 没有它，"同一事故的第三次尝试"和"三次互不相关的调查"在事件流里
    #: 长得一模一样 —— 事后统计重试效果时无法把 1 次成功归功于第 3 次尝试。
    retry_of: str | None = None
    #: 上一轮够不到的数据源（契约 payload.known_unavailable）。
    #:
    #: 必须影响**收集顺序**：预算按调用次数计，固定顺序下重试会把预算花在
    #: 上一轮已经成功的源上，还没轮到失败的那个就耗尽 —— 同一次失败重演一遍。
    known_unavailable: list[str] | None = None

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
    # ---- 影子模式 ----
    # 列在里面的弃权闸门**照常计算、照常记录，但不改变结论**。
    #
    # 与 `enable_semantic_gate=False` 的区别：那个是"不启用、也不观测"，
    # 这个是"观测但不执行"。线上校准需要后者 —— 关掉闸门跑一周等于一周没有
    # 保护；影子模式同时得到"闸门生效时的结论"和"闸门放开时的结论"。
    # 可用名见 `traceops/rca/shadow.py`：ambiguity_gate / semantic_gate /
    # refutation_gate。
    shadow_gates: frozenset[str] = frozenset()
    #: 跨调查共享的数据源健康表（`runtime/datasource_health.py`）。
    #:
    #: 由调用方显式传入并跨 run 复用 —— "跨调查"的边界就是"同一个进程"。
    #: 为 None 时不做聚合（单测/评测常用），报告里就是空列表。
    health: Any = None
    #: 部署方声明的遥测约定（指标名/标签名，`traceops.evidence.templates`）。
    #:
    #: 为 None 时用演示栈约定。换成别人的集群只需换这一个值：模板里的名字
    #: 不是事实，是某套采集配置的产物。
    conventions: TelemetryConventions | None = None
    #: 首轮因查询预算被截掉的服务数（由 `_plan_items` 写，报告读）。
    skipped_by_budget: int = 0
    #: 跨运行共享的策略变更历史（`config/history.py`）。为 None 时不做归属记录。
    strategy_history: Any = None
    #: 跨事故记忆（`traceops.runtime.incident_memory.IncidentMemory`）。
    #:
    #: 只做一件事：把"这个服务以前以这种方式坏过"变成排序里的一点先验。
    #: 它**不产生证据**：`_link_node` 看不到它，`ranking` 里也只加 1 分。
    memory: Any = None
    #: 查询结果缓存（`traceops.tools.query_cache.QueryCache`）。
    #:
    #: 默认 None = 不缓存。缓存改变的是"多久之前抓的数据可以冒充现在这份"，
    #: 必须是显式选择；而且命中时证据的 `collected_at` 会变成**当初**取数的时刻。
    cache: Any = None
    #: 调查会话记录（`traceops/runtime/session.py` 的 `SessionRecorder`）。
    #:
    #: 默认 None = 不记录：报告只写结论，写不写"怎么走到结论的"应该由部署方决定。
    #: 给定时每次调查落一份 `<run_id>.jsonl`，并在报告里留 `session_ref`。
    session: Any = None
    #: 本轮开始时观察到的策略变更（None = 与上一次观察相比没变，或没有历史）。
    #:
    #: 记在上下文里而不是节点里：一个 run 只能有一条"用的是哪套策略"，
    #: 每个节点各算一次会得到互相矛盾的结论。
    strategy_change: dict[str, Any] | None = None
    #: 数据源清单（`traceops.tools.manifest.DatasourceManifest`）。
    #:
    #: 默认 None = 演示栈清单：指标/日志/trace 三类必备、剖析可选。
    #: 它决定"查哪几类证据"以及"缺了某类证据时报告该怎么说"；
    #: 与 `conventions` 是两个问题（那份管"怎么查"，这份管"能查什么"）。
    manifest: Any = None

    def strategy_components(self) -> dict[str, Any]:
        """本次调查的**决策策略**组成（阈值、权重、闸门、提示词）。

        从 ctx 自己派生而不是由调用方传入：这些旋钮就长在 ctx 上，
        让调用方另外再传一份，两份迟早不一致 —— 而不一致的表现是
        "报告说策略没变，结论却变了"。
        """
        from traceops.runtime.strategy import strategy_components

        return strategy_components(
            enable_semantic_gate=self.enable_semantic_gate,
            enable_contradiction=self.enable_contradiction,
            shadow_gates=self.shadow_gates,
            prompt_version=self.prompt_version(),
        )

    def strategy_digest(self) -> str:
        from traceops.runtime.strategy import _digest

        return _digest(self.strategy_components())

    def observe_strategy(self) -> dict[str, Any] | None:
        """每轮开始时记一次"当前策略"，返回相对上一次观察的变更。

        必须每轮都调（不只是变更时）：历史表要能回答"这一轮相对上一轮变了什么"，
        而"上一轮"就是上一次运行，不是上一次变更。
        """
        if self.strategy_history is None:
            return None
        from traceops.config.fields import current_knobs
        from traceops.config.history import StrategyHistory
        from traceops.runtime.strategy import STRATEGY_REVISION

        history: StrategyHistory = self.strategy_history
        change = history.observe(
            knobs=current_knobs(),
            digest=self.strategy_digest(),
            revision=STRATEGY_REVISION,
        )
        self.strategy_change = change.to_dict() if change is not None else None
        return self.strategy_change

    def prompt_version(self) -> str | None:
        """只有真的挂了模型，这次 run 才用到了提示词。

        纯规则跑记一个提示词版本是**假信息**：它会让两次"都没用模型"的
        调查看起来像用了同一个提示词。
        """
        if self.model is None:
            return None
        from traceops.prompts import prompt_version

        return prompt_version()

    def prompt_part_versions(self) -> tuple[str, ...]:
        """本次运行所用提示词的逐片段版本（纯规则运行时为空）。"""
        if self.model is None:
            return ()
        from traceops.prompts import prompt_part_versions

        return prompt_part_versions()

    def run_snapshot(self, request: InvestigationRequest | None = None, *,
                     failed_sources: list[str]
                     | None = None) -> dict[str, Any]:
        from traceops.runtime.strategy import run_snapshot

        return run_snapshot(
            budget=self.budget,
            scope_depth=self.scope_depth,
            topology=self.topology,
            prompt_version=self.prompt_version(),
            prompt_parts=self.prompt_part_versions(),
            datasource_health=(
                self.health.summary() if self.health is not None else None
            ),
            config_version=getattr(request, "config_version", None),
            retry_of=getattr(request, "retry_of", None),
            known_unavailable=getattr(request, "known_unavailable", None),
            failed_sources=failed_sources,
            strategy_change=self.strategy_change,
            alert_summary_present=(
                getattr(request, "alert_summary", None) is not None
                if request is not None
                else None
            ),
        )


@dataclass
class InvestigationResult:
    status: str  # SUPPORTED | REFUTED | INCONCLUSIVE | FAILED
    report: RCAReport | None
    evidence_ids: list[str]
    errors: list[str]
    tool_calls: int
    tokens_used: int
    #: 本次调查里"查过、但确实没有数据"的查询。
    #:
    #: 它和 `errors`（没法查）是**两回事**，必须分开存：
    #:
    #: - errors → 数据源挂了，报告不能基于它做任何结论
    #: - no_data_queries → 查过了，确实没有 —— 这是**阴性观测**，
    #:   可以支持 REFUTED（"如果真是池耗尽，日志里该有超时，但查了没有"）
    #:
    #: 混在一起的后果正是 aurora-lab 记录的：把"没看见"当成"看见了"。
    no_data_queries: list[str] = field(default_factory=list)


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
    #: "查过、确实没有"的查询（与 errors 分开，见 InvestigationResult.no_data_queries）
    no_data_queries: list[str]
    #: 被校验或去重丢掉的模型提案（带原因码）。
    #:
    #: 必须声明在 TypedDict 里 —— LangGraph 只合并这里列出的键，未声明的键
    #: 会被静默丢弃，表现为"节点明明 append 了、报告里永远是空列表"。
    rejected_proposals: list[dict[str, Any]]
    #: 被降级读取的原始数据（落盘失败/读不回来），每项带替代读取路径。
    degraded_reads: list[dict[str, Any]]
    #: 本轮够不到的数据源 id（进终态事件给控制面，供下一轮重试先查它们）。
    failed_sources: list[str]
    cache_hits: list[dict[str, Any]]
    #: 已经取证了几趟（判别回路的计数）。与预算的 `rounds` 同一语义：
    #: 一次 `collect` = 一趟。没有它，回路无法知道自己该不该再问。
    round: int
    #: 当前计划的阶段：`probe`（首轮全量）/ `discriminate`（为分开两个候选而问）。
    phase: str
    #: 已经判别过哪些 `fault_type:service`，避免同一对候选被反复问。
    discriminated: list[str]
    #: `_rank_node` 算出的"下一趟要不要问、问什么"。路由函数读它决定
    #: 走判别还是直接出报告 —— 路由函数拿不到 RunContext（也就看不到预算），
    #: 所以这个判断必须在上一个节点里做完。
    can_discriminate: bool
    next_plan: list[dict[str, Any]]


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
    """首轮（probe）计划：每个在范围内的服务查常规三类数据源。

    刻意保持"全量"而不是"先只查 error_rate"：评测里确实存在只有日志里
    才看得出根因的场景，省那两次调用换来的是一半场景直接查不到。
    真正该省的地方是"两个候选分不开时多问一趟"（见 `_discrimination_decision`），
    而不是"一开始就少查几类数据"。
    """
    items: list[dict[str, Any]] = []
    templates = [(tool, source_id, evidence_type, template_id)
                 for tool, source_id, evidence_type, template_id in _PLAN_TEMPLATES
                 if _capability_planned(ctx, _PLAN_TEMPLATE_CAPABILITY[tool])]
    scoped = _scope_services(request, ctx)
    # 计划的开销是「服务数 × 数据源数」，而预算是常数。真实集群里上百个服务
    # 会让首轮取证一条都跑不完就耗尽预算（实测 13 个服务 × 3 类 = 39 次 > 40
    # 的默认上限，调查直接 FAILED）—— 那不是"数据查不到"，是"根本没开始"。
    # 于是按作用域顺序（先自身、再下游、最后影响面）截断，并把被截掉的数量
    # 交给报告，而不是静默少查。
    wanted = max(1, ctx.budget.tool_calls_remaining // max(1, len(templates)))
    kept = scoped[:wanted]
    ctx.skipped_by_budget = max(0, len(scoped) - len(kept))
    for service, tier in kept:
        base = {
            "service": service,
            "start": request.window_start.isoformat(),
            "end": request.window_end.isoformat(),
        }
        for tool, source_id, evidence_type, template_id in templates:
            template_params: dict[str, Any] = {
                "service": service, "start": base["start"], "end": base["end"],
            }
            if tool == "query_service_traces":
                template_params["min_duration_ms"] = SLOW_SPAN_THRESHOLD_MS
            # 模板渲染失败 = 计划非法：宁可少查一个数据源，也不发出未校验的查询
            try:
                rendered_query = default_registry(ctx.conventions).build(
                    template_id, template_params
                )
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
    items.extend(_change_items(request))
    return items


def _adapter_supports(ctx: "RunContext", capability: str) -> bool:
    """适配器是否声明了这项能力。

    不声明就不进计划：`query_service_profile` 的实现是
    `getattr(adapters, "query_service_profile", None) or _unconfigured(...)`，
    没有 Pyroscope 的部署每次调查都会多出一条 "profile: not configured"
    的 missing_data —— 噪声会被当成"数据缺失"进报告，稀释真正的缺失。
    """
    adapters = getattr(ctx, "adapters", None)
    declared = getattr(adapters, "capabilities", frozenset()) if adapters is not None else frozenset()
    return capability in declared


#: 计划模板 → 它属于哪一类证据（与 `tools/manifest.py` 的能力词一致）。
_PLAN_TEMPLATE_CAPABILITY: dict[str, str] = {
    "query_service_error_rate": "metric",
    "query_service_logs": "log",
    "query_service_traces": "trace",
}


def _active_manifest(ctx: "RunContext") -> Any:
    """本次调查的数据源清单。None → 演示栈清单（三类必备 + 剖析可选）。"""
    from traceops.tools.manifest import default_manifest

    manifest = getattr(ctx, "manifest", None)
    return manifest if manifest is not None else default_manifest()


def _capability_planned(ctx: "RunContext", capability: str) -> bool:
    """这一类证据该不该进计划。

    两个条件同时成立才查：**部署方声明了它**（清单）且**运行时确实能查它**
    （适配器没有显式否认）。缺任一条就跳过 —— 跳过时报告会通过
    `datasource_gaps` 说清"没查是因为这个部署没有这个源"，而不是记一条
    "查了没结果"。后者会被当成阴性证据用来支持 `REFUTED`，而"这个集群
    根本不产这类数据"什么也证明不了。
    """
    from traceops.tools.manifest import present_capabilities

    if not _active_manifest(ctx).supports(capability):
        return False
    available = present_capabilities(getattr(ctx, "adapters", None))
    # 适配器没声明能力表（测试替身、老代码）：不否认即视为能查。
    return available is None or capability in available


def _change_items(request: InvestigationRequest) -> list[dict[str, Any]]:
    """变更流水 → 合成事件证据项（不经过数据源）。"""
    records: list[dict[str, Any]] = []
    if request.config_change is not None:
        records.append({**dict(request.config_change), "change_kind": "config"})
    records.extend(dict(change) for change in (request.changes or []))

    items: list[dict[str, Any]] = []
    for record in records:
        try:
            changed_at = _parse_dt(record["changed_at"])
        except (KeyError, TypeError, ValueError):
            # 一条坏记录不该让整趟调查失败：变更流水是外部输入，
            # 而它提供的只是"可能需要看的方向"，不是证据本身。
            continue
        service = record.get("service")
        items.append(
            {
                "tool": "config_change_event",
                "params": {},
                "source_id": "control-plane",
                "evidence_type": EvidenceType.EVENT,
                "query_template_id": (
                    "qt-deployment-v1" if record.get("change_kind") == "deployment"
                    else "qt-config-change-v1"
                ),
                "service": service,
                "payload": record,
                "observed_at": changed_at,
            }
        )
    return items


def _profile_items(request: InvestigationRequest, ctx: "RunContext",
                   services: list[str]) -> list[dict[str, Any]]:
    """持续剖析（PROFILE）证据的取数项，只为**判别阶段点名的服务**构造。

    为什么不做成常规模板：profile 的查询按服务数线性增加，
    而它回答的问题（"慢在哪个函数"）只有在已有 `latency_degradation`
    这类假设、且需要区分两个候选时才有价值。常规就查等于每次都为一个
    大概率用不上的维度付费。

    为什么要求适配器声明能力：见 `_adapter_supports`。
    """
    if not _capability_planned(ctx, "profile") or not _adapter_supports(ctx, "profile"):
        return []
    tool, source_id, evidence_type, template_id = _DEEPEN_TEMPLATES[0]
    start = request.window_start.isoformat()
    end = request.window_end.isoformat()
    items: list[dict[str, Any]] = []
    for service in services:
        if not service:
            continue
        items.append(
            {
                "tool": tool,
                "params": {"service": service, "start": start, "end": end},
                "source_id": source_id,
                "evidence_type": evidence_type,
                "query_template_id": template_id,
                "service": service,
                "tier": INVESTIGATION_TIER,
                # 这条查询不走模板库的渲染路径（形状固定："某服务某窗口的火焰图"），
                # 但 `query_template_id` 是 Evidence 的必填字段 —— 给一个稳定标签，
                # 让审计能按它把同一类查询归到一起。
                "rendered_query": f"pyroscope: flamegraph(service={service}, {start}..{end})",
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
    if evidence_type is EvidenceType.PROFILE:
        # Pyroscope 形态：{"data": {"flamegraph": {"names","values"}}}
        # 只在外面包了一层的时候拆掉；已经是内层形状就原样交给 Normalizer
        if isinstance(raw.get("data"), dict) and "flamegraph" in raw["data"]:
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


def _is_empty_payload(raw: Any) -> bool:
    """这次查询是不是「查了，但什么都没有」。

    判据与 `ToolGateway._is_empty_result` 同源 —— 那一份是"什么算空"的
    **唯一**真值：Gateway 用它区分 OK / NO_DATA 做审计，这里用它区分
    "有证据" / "没有数据"。两处若各写一套，形状迟早漂移，
    于是审计说 NO_DATA、编排却造出一条空证据（这正是当前存在的缺陷）。

    这里判的是**归一化之后**的形状（`_unwrap_raw` 已剥掉外层 status/data），
    而 Gateway 判的是原始响应。两者覆盖的真实形状一致：
      - Prometheus matrix: {"resultType": "matrix", "result": []}
      - Loki streams:      {"streams": []}
      - Tempo batches:     {"batches": []}
    """
    if raw is None:
        return True
    if isinstance(raw, (str, list, dict, tuple, set)) and len(raw) == 0:
        return True
    if isinstance(raw, dict):
        for key in ("result", "records", "spans", "series", "batches", "streams", "traces"):
            value = raw.get(key)
            if isinstance(value, (list, tuple)) and len(value) == 0:
                return True
        if raw.get("traces_considered") == 0:
            return True
    return False


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

def _budget_guard(fn, *, counts_a_round: bool):
    @functools.wraps(fn)
    def wrapper(state, config):
        ctx = _ctx_from(config)
        try:
            if counts_a_round:
                ctx.budget.begin_round()
            else:
                ctx.budget.check_deadline()
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


def _charged(fn):
    """**取证的**节点：执行一次算一轮，并在入口检查 round / deadline 预算。

    只有真正去问数据源的节点算轮（`plan` 之后的 `collect`、判别阶段的
    `discriminate` 带出的下一趟 `collect`）。计划、推理、排序、出报告这些
    节点只检查 deadline —— 它们不花工具调用，把它们算进轮数会让
    `max_rounds` 限制的变成"跑过几个节点"而不是"问过几次数据源"。
    """
    return _budget_guard(fn, counts_a_round=True)


def _timed(fn):
    """不取证但也不该无限运行的节点：只受墙钟上限约束。"""
    return _budget_guard(fn, counts_a_round=False)


def _prioritise_plan(plan: list[dict[str, Any]],
                     known_unavailable: list[str] | None) -> list[dict[str, Any]]:
    """把"上一轮够不到"的源排到最前，其余保持原顺序。

    只改顺序、不改条数：它不能凭空多出证据，只能决定预算先花在哪。
    排序必须稳定 —— 同一份 plan 重排两次得到同一结果，否则同一事故的
    两次重试之间连"查了什么"都无法对比。
    """
    if not known_unavailable:
        return list(plan)
    unavailable = set(known_unavailable)
    first = [item for item in plan if item.get("source_id") in unavailable]
    if not first:
        return list(plan)
    rest = [item for item in plan if item.get("source_id") not in unavailable]
    return first + rest


@_timed
def _plan_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    request = state["request"]
    plan = _plan_items(request, _ctx_from(config))
    return {"plan": _prioritise_plan(plan, request.known_unavailable)}


@_charged
def _collect_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    request = state["request"]
    ctx = _ctx_from(config)
    gateway = ctx.gateway or _build_gateway(ctx)
    ctx.gateway = gateway
    evidence_list: list[Evidence] = []
    missing = list(state.get("missing_data", []))
    no_data = list(state.get("no_data_queries", []))
    degraded = list(state.get("degraded_reads", []))
    failed_sources = list(state.get("failed_sources", []))
    cache_notes = list(state.get("cache_hits", []))

    for item in state["plan"]:
        # 内置工具走 Gateway（权限/限额/审计）；事件证据由控制面事件转换而来，不调工具
        spilled_ref: dict[str, Any] | None = None
        # 判据是"这一项自带 payload"，不是工具名 —— 变更流水里的部署/扩缩容
        # 走的是同一个合成路径，但它们的工具名不同（见 _change_items）。
        if "payload" in item:
            payload = dict(item["payload"])
            raw = {"event": {**payload, "type": payload.get("change_kind") or "config_change"}}
            observed_at = item["observed_at"]
            window = (observed_at, observed_at)
        else:
            try:
                raw = gateway.invoke(item["tool"], item["params"], budget=ctx.budget)
                cache_note = gateway.take_cache_note()
            except BudgetExhausted as exc:
                errors = list(state.get("errors", [])) + [f"budget exhausted: {exc}"]
                return {"status": "FAILED", "errors": errors, "missing_data": missing}
            except Exception as exc:  # 单数据源失败不终止调查，记入 missing_data
                # 必须留日志：missing_data 只进最终报告的 "missing" 列表，
                # 运维在 worker 日志里看不到它，也不知道是哪个数据源、什么异常。
                # 静默 continue 在这里等于"证据凭空消失"。
                log.warning("datasource query failed; evidence skipped",
                            extra={"tool": item["tool"],
                                   "error_type": type(exc).__name__,
                                   "reason": str(exc),
                                   "run_id": request.investigation_run_id,
                                   "incident_id": request.incident_id},
                            exc_info=True)
                _session_record(
                    ctx, "tool_call", tool=item["tool"], params=item["params"], ok=False,
                    payload_bytes=None, cache_hit=False,
                    error=f"{type(exc).__name__}: {str(exc)[:300]}",
                )
                if ctx.health is not None:
                    # 记进**跨调查**的健康表：单次 missing_data 说不清
                    # "这个数据源今天一直不行"，而这正是 abstention 归因的关键。
                    ctx.health.record_failure(item["source_id"], f"{type(exc).__name__}: {exc}")
                missing.append(f"{item['tool']}: {exc}")
                # 手工构造的 plan 项（测试/兼容路径）可能没有 source_id：
                # 这里用 get，宁可不记也不要因为一个可选字段把整轮调查炸掉。
                source_id = item.get("source_id")
                if source_id and source_id not in failed_sources:
                    failed_sources.append(source_id)
                continue
            _session_record(
                ctx, "tool_call", tool=item["tool"], params=item["params"], ok=True,
                payload_bytes=_payload_size(raw),
                cache_hit=cache_note is not None, error=None,
            )
            if cache_note is not None:
                # 命中缓存必须留在报告里：这份证据的 `collected_at` 是**当初**
                # 取数的时刻，读者要能看出它不是刚抓的。
                cache_notes.append({**cache_note, "service": item.get("service")})
            if ctx.health is not None:
                # "查到了但没有数据"是成功：数据源是通的，这是阴性证据。
                ctx.health.record_success(item["source_id"])

            # **超限结果必须先读回再归一化**。
            #
            # Gateway 对超过 max_result_bytes 的结果返回的是指针
            # `{_spill: {...}}`，不是数据本身。此前这里直接把指针交给
            # Normalizer，三个归一化器全部拒绝，ValueError 落进
            # missing_data —— 一次完全成功的查询，只因为数据量大，
            # 变成了"缺失数据"。证据覆盖率下降的根因居然只是体积。
            #
            # 落盘保护的是**上下文**，不是证据链：归一化在本进程内完成，
            # 全量读回不占模型 token，所以这里读全量是对的。
            if gateway.is_spill_reference(raw):
                try:
                    spilled_ref = raw[SPILL_REF_KEY]
                    raw = gateway.load_spilled(raw)
                except Exception as exc:
                    log.warning("spilled datasource result could not be read back",
                                extra={"tool": item["tool"],
                                       "error_type": type(exc).__name__,
                                       "reason": str(exc),
                                       "run_id": request.investigation_run_id},
                                exc_info=True)
                    missing.append(
                        f"{item['tool']}: result was spilled but unreadable ({exc})")
                    # 光记"读不回来"是不够的：复盘的人需要知道数据还能怎么拿到。
                    # `read` 是这条降级的替代读取路径，缺失时也要如实说没有。
                    degraded.append({
                        "kind": "spill_unreadable",
                        "tool": item["tool"],
                        "bytes": (spilled_ref or {}).get("bytes"),
                        "reason": str(exc)[:200],
                        "read": gateway.read_path(raw) or "unavailable",
                    })
                    continue
            unwrapped = _unwrap_raw(raw, item["evidence_type"])
            observed_at = _observed_at(unwrapped, item["evidence_type"], request.window_end)
            window = (request.window_start, request.window_end)
            raw = unwrapped

        # **「查了，没有」必须在归一化之前判定**。
        #
        # 归一化器对空输入一律拒绝（metric/log/trace 三个都是），
        # 那是刻意的：空结果不是证据。如果放任它抛到下面的 except，
        # 就会被记成 missing_data（"没法查"）—— 语义正好反了：
        # 我们明明查了，确实没有。
        #
        # 判据复用 Gateway 的 `_is_empty_result`（同一份"什么算空"的真值），
        # 不在这里另写一套形状判断 —— 两份定义必然漂移。
        if "payload" not in item and _is_empty_payload(raw):
            no_data.append(
                f"{item['tool']}[{item.get('service', request.service_hint)}]: "
                "query succeeded but returned no data"
            )
            log.info("datasource query returned no data; no evidence created",
                     extra={"tool": item["tool"],
                            "service": item.get("service", request.service_hint),
                            "run_id": request.investigation_run_id,
                            "incident_id": request.incident_id})
            continue

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
                # 命中缓存时，"我们什么时候拿到这份数据"是**当初**那一刻。
                # 明细见 tools/query_cache.py 的模块注释。
                collected_at=_cache_fetch_time(cache_note),
            )
        except Exception as exc:  # 形状异常 → 记为缺失数据，不伪造证据
            missing.append(f"{item['tool']}: {exc}")
            continue

        # 超限结果把落盘指针挂到证据上：证据本身是**归一化后的摘要**
        # （`normalized_data` 有记录数上限），全量原文在 spill 文件里。
        # 不记这个指针的话，报告引用了一条证据、却说不出"全量在哪" ——
        # 那正是「落盘即黑洞」的一半症状。另一半（读回）在上面的 load_spilled。
        if spilled_ref is not None and spilled_ref.get("path"):
            evidence.raw_snapshot_ref = str(spilled_ref["path"])

        ctx.store.register_source(item["source_id"])
        ctx.store.add(evidence)
        evidence_list.append(evidence)

    # **证据必须累加，不能整键替换**。
    #
    # LangGraph 对没有 reducer 的键是"后写覆盖"语义：判别回路里第二趟 collect
    # 若只返回本轮新证据，第一趟的证据会凭空消失 —— 表现为"多查了一次之后
    # 报告里原来的证据没了"，而每一步单独看都对。
    carried = list(state.get("evidence", []))
    return {"evidence": carried + evidence_list, "missing_data": missing,
            "no_data_queries": no_data, "degraded_reads": degraded,
            "failed_sources": failed_sources, "cache_hits": cache_notes}


@_timed
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
    rejected_proposals: list[dict[str, Any]] = []

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
            for rejected in response.rejected:
                rejected_proposals.append({
                    "candidate_service": rejected.candidate_service,
                    "fault_type": rejected.fault_type,
                    "reason": rejected.reason,
                    "reason_text": REJECTION_REASONS.get(rejected.reason, rejected.reason),
                })
            for proposal in response.hypotheses:
                key = (proposal.candidate_service, proposal.fault_type, proposal.candidate_component)
                if key in existing:
                    # 与规则假设重复不产生并列候选，但必须留痕 —— 否则"模型提了
                    # 但被去重"和"模型没提"在事后无法区分。
                    rejected_proposals.append({
                        "candidate_service": proposal.candidate_service,
                        "fault_type": proposal.fault_type,
                        "reason": "duplicate_of_rule_hypothesis",
                        "reason_text": REJECTION_REASONS["duplicate_of_rule_hypothesis"],
                    })
                    continue
                existing.add(key)
                hypotheses.append(hypothesis_to_domain(
                    proposal, investigation_run_id=request.investigation_run_id))

    if not hypotheses:
        missing.append("no candidate hypotheses generated from collected evidence")
    return {
        "hypotheses": hypotheses,
        "missing_data": missing,
        "rejected_proposals": rejected_proposals,
    }


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
        elif evidence.evidence_type is EvidenceType.PROFILE:
            hottest = _hottest_frame(data)
            if hottest is not None:
                frame, self_value = hottest
                entry["hot_function"] = frame
                entry["hot_self_value"] = self_value
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


def _payload_size(payload: Any) -> int | None:
    """返回体字节数（只记大小，不记内容：载荷可能有几百 KB）。"""
    if payload is None:
        return None
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _session_ref(ctx: Any) -> str | None:
    """报告里指回会话文件的路径（没开会话记录时为 None）。"""
    session = getattr(ctx, "session", None)
    if session is None or not getattr(session, "enabled", False):
        return None
    path = getattr(session, "path", None)
    return str(path) if path is not None else None


def _session_record(ctx: Any, kind: str, **fields: Any) -> None:
    """记一条会话事件。**永不抛异常** —— 记录失败不该让调查失败。"""
    session = getattr(ctx, "session", None)
    if session is None:
        return
    try:
        session.record(kind, **fields)
    except Exception as exc:  # noqa: BLE001 - 记录失败是外部问题，不是调查问题
        log.warning("session record failed", extra={"kind": kind, "reason": str(exc)})


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


def _cache_fetch_time(note: dict[str, Any] | None) -> datetime | None:
    """从缓存命中记录里取回原始抓取时刻（没有命中或格式坏掉时 None）。"""
    if not note:
        return None
    try:
        return datetime.fromisoformat(note["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return None


def _hottest_frame(data: dict[str, Any]) -> tuple[str, float] | None:
    """火焰图里自身开销最大的那一帧。

    为什么给函数名而不是整份火焰图：火焰图动辄上千帧、几十 KB，
    而模型的用途只有一个 —— "这个服务的 CPU 时间花在哪"。
    函数名是结构化字段（不是日志自由文本），可以安全进 Prompt。
    """
    frames = data.get("hot_frames") or []
    if not frames:
        return None
    top = frames[0]
    name = str(top.get("frame") or "").strip()
    if not name:
        return None
    try:
        self_value = float(top.get("self_value") or 0.0)
    except (TypeError, ValueError):
        self_value = 0.0
    return name, self_value


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


@_timed
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
                # 关系类型由矛盾模块定义 —— 不在这里硬编码常量，
                # 否则两处独立真值，改一处就会静默漂移。
                relation_type=relation_type_of(item),
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


@_timed
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


def _ambiguous_candidates(state: InvestigationState,
                          ranked: list[Any] | None = None) -> list[tuple[str, str]]:
    """并列首名的候选，返回 (candidate_service, fault_type) 列表。

    判据是"分数**完全相同**且落在**不同服务**上"，不是"分数差在阈值内"：
    排序规则是离散加权的，差 1 分已经意味着"有一条证据支持 A 而 B 没有"，
    那是可判别的差异，不该再花工具调用去问。同一服务的两种机制并列
    也不值得再问 —— 它只影响措辞，不影响定位。
    """
    from traceops.domain.models import HypothesisStatus

    # 显式传入刚算出的 `ranked`：在 `_rank_node` 里读 `state["ranked"]` 拿到的是
    # **上一趟**的结果（首趟是空列表），于是"并列"永远判不出来 —— 回路形同不存在。
    # 这个 bug 是判别回路的测试抓到的：图看起来有环，实际一次都没进去过。
    ranked = list(ranked if ranked is not None else (state.get("ranked") or []))
    by_id = {h.id: h for h in (state.get("hypotheses") or [])}
    supported = [r for r in ranked if r.status is HypothesisStatus.SUPPORTED]
    if len(supported) < 2:
        return []
    top_score = max(r.score for r in supported)
    group = [r for r in supported if r.score == top_score]
    out: list[tuple[str, str]] = []
    services: set[str] = set()
    for candidate in group:
        hypothesis = by_id.get(candidate.hypothesis_id)
        if hypothesis is None:
            continue
        out.append((hypothesis.candidate_service, hypothesis.fault_type))
        services.add(hypothesis.candidate_service)
    if len(services) < 2:
        return []
    return out


def _focused_window(request: InvestigationRequest,
                    anchor: datetime | None) -> tuple[str, str]:
    """以最早异常为中心的窄窗口，夹在请求的原窗口内。

    夹取是必需的：证据时间戳可能落在请求窗口外（数据源的最小步长、
    或 client 报了个略早的时间），把窗口扩出去会让查询变成"查了我没被授权的
    那段时间" —— 采集层的窗口必须始终是请求窗口的子集。
    """
    center = anchor or request.fault_start
    start = max(center - timedelta(seconds=FOCUSED_WINDOW_SECONDS), request.window_start)
    end = min(center + timedelta(seconds=FOCUSED_WINDOW_SECONDS), request.window_end)
    if end <= start:  # 极短窗口：退回请求窗口，别发出空区间
        return request.window_start.isoformat(), request.window_end.isoformat()
    return start.isoformat(), end.isoformat()


def _discrimination_items(request: InvestigationRequest, ctx: "RunContext",
                          services: list[str], *, anchor: datetime | None,
                          latency_services: list[str]) -> list[dict[str, Any]]:
    """判别轮的取数项：对**并列的每个服务**问一次它自己的错误率（聚焦窗口）。

    选错误率而不是更"专业"的指标：它对每个服务都存在（不需要额外部署）、
    它就是"这个服务此刻在不在坏"，而且它的异常判定是现成的
    （`metric_is_anomalous` 认 error-rate 模板）。
    只对需要的服务付这次钱 —— 判别是"两个候选之间"的事情，
    不是"把全部服务再查一遍"。
    """
    start, end = _focused_window(request, anchor)
    items: list[dict[str, Any]] = []
    for service in services:
        if not service:
            continue
        items.append(
            {
                "tool": "query_service_error_rate",
                "params": {"service": service, "start": start, "end": end},
                "source_id": "prometheus",
                "evidence_type": EvidenceType.METRIC,
                # 名字里必须含 error-rate：检测器与语义闸门都按它识别"这是错误率"。
                "query_template_id": "qt-error-rate-focused",
                "service": service,
                "tier": INVESTIGATION_TIER,
                # 与 qt-error-rate-v1 共用同一个渲染函数：这里的 PromQL 曾经是
                # 手抄的第二份，改约定时会漏掉它，于是"首轮查得到、判别轮查不到"。
                "rendered_query": render_error_rate(
                    service, ctx.conventions or TelemetryConventions()
                )
                + f"  # focused ±{FOCUSED_WINDOW_SECONDS}s",
            }
        )
    items.extend(_profile_items(request, ctx, latency_services))
    return items


def _discrimination_decision(state: InvestigationState,
                            ctx: "RunContext",
                            *,
                            ranked: list[Any] | None = None) -> tuple[bool, list[dict[str, Any]]]:
    """要不要再问一趟、问什么。

    四件事只有这里知道，所以必须在这里一次算完：
      - 谁跟谁并列（`_ambiguous_candidates`）；
      - 还剩几轮（`round`）与还剩几次工具调用（`ctx.budget`）——
        路由函数拿不到 RunContext，把判断放进去就等于让连线做决策；
      - 每对候选已经问过什么（`discriminated`）—— 问第二遍同样的查询
        只是烧钱，不会多出证据；
      - 以哪个时间点为窗口中心（最早异常）。
    """
    if int(state.get("round", 0)) >= DISCRIMINATION_ROUNDS:
        return False, []
    # 判别回路要问的是 prometheus（聚焦错误率）与 pyroscope（火焰图）。
    # 这两条路上一趟已经失败过就别再问：那不是"分不开"，是"问不到" ——
    # 再问一遍只会把预算花在同一个已知的坏数据源上，还会在报告里多两条
    # missing_data 噪音。
    failed = set(state.get("failed_sources") or [])
    if "prometheus" in failed:
        return False, []
    ambiguous = _ambiguous_candidates(state, ranked)
    if not ambiguous:
        return False, []
    request = state["request"]
    already = set(state.get("discriminated", []))
    services: list[str] = []
    latency_services: list[str] = []
    for service, fault_type in ambiguous:
        if not service or service in already:
            continue
        if service not in services:
            services.append(service)
        if fault_type == "latency_degradation":
            latency_services.append(service)
    if not services:
        return False, []
    earliest = earliest_anomaly(state.get("evidence", []))
    items = _discrimination_items(
        request, ctx, services,
        anchor=earliest.observed_at if earliest is not None else None,
        latency_services=latency_services,
    )
    # 其余数据源同理：单个源挂了就只问还能问的那几条。
    if failed:
        items = [item for item in items if item.get("source_id") not in failed]
        if not items:
            return False, []
    # 预算不够就只问能问的那几条：问一半也好过因为排不下而一条都不问，
    # 而且截断发生在**构造计划时**，不是问了之后才发现超预算。
    remaining = ctx.budget.tool_calls_remaining
    if len(items) > remaining:
        items = items[:remaining]
    return bool(items), items


@_timed
def _rank_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    # 每个 return 分支都必须显式重置这两个键：它们决定 rank 之后走不走回路，
    # 而 state 跨趟累积 —— 漏掉一个分支就会带着上一趟的 True 无限循环。
    request = state["request"]
    hypotheses = state.get("hypotheses", [])
    if not hypotheses:
        return {"ranked": [], "can_discriminate": False, "next_plan": []}
    # R-009 需要"全网最早异常"这个跨候选的参照点：单个候选看不到别的候选，
    # 规则本身也只能算绝对分。这里算一次，分给每个候选自己比对。
    earliest = earliest_anomaly(state.get("evidence", []))
    earliest_at = earliest.observed_at if earliest is not None else None
    memory = _ctx_from(config).memory
    signals = [
        derive_signals(
            h, state.get("links", []), _ctx_from(config).store,
            fault_start=request.fault_start, earliest_anomaly_at=earliest_at,
            previously_seen=_previously_seen(memory, h),
        )
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

    can_discriminate, next_plan = _discrimination_decision(
        state, _ctx_from(config), ranked=ranked)
    return {"ranked": ranked, "can_discriminate": can_discriminate, "next_plan": next_plan}


@_timed
def _discriminate_node(state: InvestigationState, config: RunnableConfig) -> dict[str, Any]:
    """把 rank 算好的判别计划变成下一趟的 plan。

    为什么判别是独立节点而不是塞进 `_rank_node`：`plan` 是 collect 的输入，
    rank 之前改它没有意义（rank 自己要看的就是本轮证据）。一个节点只做一件事，
    也让"图长什么样"能在代码里一眼看出来 —— 判别回路的存在必须可见。
    """
    plan = list(state.get("next_plan") or [])
    if not plan:
        # 路由只在 `can_discriminate and next_plan` 时才进来，走到这里说明
        # 两个键被写坏了。静默返回会让 collect 重跑旧计划（重复取证），
        # 或者让图原地打转 —— 两种都比大声报错更难查。
        raise RuntimeError("discrimination node reached without a plan")
    attempted = list(state.get("discriminated") or [])
    for item in plan:
        service = item.get("service")
        if service and service not in attempted:
            attempted.append(service)
    return {
        "plan": plan,
        "phase": "discriminate",
        "round": int(state.get("round", 0)) + 1,
        "discriminated": attempted,
        "can_discriminate": False,
        "next_plan": [],
    }


def _route_after_rank(state: InvestigationState) -> str:
    """并列且还有额度 → 再问一趟；否则直接出报告（可能以 INCONCLUSIVE 收场）。"""
    if state.get("status") == "FAILED":
        return "report"
    if state.get("can_discriminate") and state.get("next_plan"):
        return "discriminate"
    return "report"


def _previously_seen(memory: Any, hypothesis: Any) -> bool:
    """这个 (服务, 机制, 组件) 以前真的这样坏过吗。

    只认**净值大于零**：一个候选被反复推翻时，"它又来了"不该再加分 ——
    否则记忆会把调查锁死在同一个错误答案上（自证循环）。
    """
    if memory is None:
        return False
    try:
        prior = memory.recall(
            service=hypothesis.candidate_service,
            fault_type=hypothesis.fault_type or "",
            component=hypothesis.candidate_component,
        )
    except (sqlite3.Error, OSError, ValueError) as exc:
        # 只吞"存储/数据"类失败：记忆是辅助信号，读不出来不该拖垮调查。
        # 但**不吞编程错误** —— 一个 TypeError/AttributeError 说明这段代码本身写错了，
        # 把它也变成"没有先验"会让 bug 永久隐身（每次调查都少了这个信号，无人察觉）。
        log.warning("incident memory could not be read: %s", exc, exc_info=True)
        return False
    return bool(prior is not None and prior.corroborated)


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


@_timed
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
    _report_missing_topology(state, ctx, missing)
    _report_missing_sources(ctx, missing)
    skipped = getattr(ctx, "skipped_by_budget", 0)
    if skipped:
        missing.append(
            f"{skipped} service(s) in the blast radius were not queried at all: "
            f"the first pass spends one query per service per data source and the "
            f"tool-call budget ran out first (raise max_tool_calls or narrow scope_depth)"
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
        no_data=list(state.get("no_data_queries", [])),
        degraded_reads=list(state.get("degraded_reads", [])),
        affected_services=_affected_services(state, config),
        correlation_notes=_correlation_notes(state),
        propagation_chain=_propagation_chain(state, ctx),
        incident_id=request.incident_id,
        topology=ctx.topology,
        rejected_proposals=list(state.get("rejected_proposals", [])),
        semantic_checks=(
            {
                check.hypothesis_id: check
                for check in check_all(
                    list(state.get("hypotheses", [])),
                    list(state.get("evidence", [])),
                    topology=ctx.topology,
                )
            }
            if ctx.enable_semantic_gate or "semantic_gate" in ctx.shadow_gates else None
        ),
        shadow_gates=ctx.shadow_gates,
        # 策略指纹进报告：两次结论不同时，先比 digest 再谈数据。
        # 只放进 ctx 或只打日志都不够 —— 报告才是会被归档、被引用、
        # 被拿去和上次比较的那份东西。
        strategy_digest=ctx.strategy_digest(),
        run_snapshot=ctx.run_snapshot(
            request, failed_sources=list(state.get("failed_sources", []))),
        priors=_priors(state, ctx),
        topology_drift=_topology_drift(state, ctx),
        cache_hits=list(state.get("cache_hits", [])),
        session_ref=_session_ref(ctx),
        datasource_gaps=_datasource_gaps(ctx),
    )
    _remember_outcome(state, ctx)
    return {"report": report, "status": report.diagnosis_status}


def _report_missing_topology(state: InvestigationState, ctx: RunContext,
                             missing: list[str]) -> None:
    """把拓扑缺口写进 missing_data。

    为什么必须写：拓扑里没有的边，`blast_radius` 不会覆盖到，于是它
    既不进影响范围、也不进 `_unobserved_related`（那份清单是从拓扑推的）。
    一个"拓扑里根本不存在"的依赖导致的全链路超时，在报告里连
    "我没查过它"都不会出现 —— 那是最糟的沉默。
    """
    drift = _topology_drift(state, ctx)
    undeclared = drift.get("undeclared_edges") or []
    if undeclared:
        pairs = ", ".join(f"{caller}→{callee}" for caller, callee in undeclared)
        missing.append(
            "observed call edges are missing from the declared topology "
            f"(the blast radius could not include them): {pairs}"
        )
    unobserved = drift.get("unobserved_declared_edges") or []
    if unobserved:
        pairs = ", ".join(f"{caller}→{callee}" for caller, callee in unobserved)
        missing.append(
            "declared edges were not exercised in this window "
            f"(not evidence that they are down): {pairs}"
        )


def _datasource_gaps(ctx: RunContext) -> list[dict[str, Any]]:
    """清单里声明了、运行时却没有的源。

    与"查了没结果"是两件事：前者说明这一整类证据在本部署里不存在，
    后者说明看过而且没东西。分辨不出来的时候，报告会把缺源读成阴性证据。
    """
    from traceops.tools.manifest import present_capabilities

    return _active_manifest(ctx).gaps(present_capabilities(getattr(ctx, "adapters", None)))


def _report_missing_sources(ctx: RunContext, missing: list[str]) -> None:
    """把缺源写进 missing_data（只写 `required` 的）。

    可选源（例如剖析）缺失不影响结论成立与否，写进 missing_data 只会稀释
    真正重要的缺失；但它们仍然出现在报告的 `datasource_gaps` 里 —— 想知道
    "还能查什么"的人该看见全部缺口，想知道"这份结论少看了什么"的人
    只该看见必需的。
    """
    for gap in _datasource_gaps(ctx):
        if not gap.get("required"):
            continue
        missing.append(
            f"data source '{gap['source']}' is declared but unavailable: {gap['when_absent']}")
    # 没声明的基线证据类别也要说：读者默认"报告没提就是查过没事"，
    # 而这个部署里这类证据根本不可能存在（不是这次没查到）。
    for capability in _active_manifest(ctx).absent_baseline_capabilities():
        missing.append(
            f"this deployment declares no {capability} source, so {capability} evidence "
            f"cannot appear in this report (a deployment fact, not a finding)")


def _topology_drift(state: InvestigationState, ctx: RunContext) -> dict[str, Any]:
    """声明的拓扑与本窗口实际用到的调用关系对比。

    只对"观测到但没声明"的边报缺口 —— 那是真信号（拓扑缺了这条边，
    `blast_radius` 覆盖不到它）。反向的"声明了但没用到"只进 missing_data，
    因为沉默不等于断裂。详见 `traceops/topology/discovery.py` 的模块注释。
    """
    if ctx.topology is None:
        return {}
    observed = discover_edges(state.get("evidence", []))
    return diff_topology(ctx.topology, observed).to_dict()


def _priors(state: InvestigationState, ctx: RunContext) -> list[dict[str, Any]]:
    """把用到的先验写进报告。

    不写的话，"这次结论是被历史推了一把"就只存在于代码路径里 ——
    复盘时看到根因是 order-service，无法区分"证据指向它"和"它以前老出事"。
    """
    if ctx.memory is None:
        return []
    priors: list[dict[str, Any]] = []
    for hypothesis in state.get("hypotheses", []) or []:
        try:
            prior = ctx.memory.recall(
                service=hypothesis.candidate_service,
                fault_type=hypothesis.fault_type or "",
                component=hypothesis.candidate_component,
            )
        except (sqlite3.Error, OSError, ValueError) as exc:
            # 同 `_previously_seen`：存储失败可以降级，编程错误必须炸出来。
            log.warning("incident memory could not be read while building priors: %s",
                        exc, exc_info=True)
            continue
        if prior is None or not prior.corroborated:
            continue
        entry = prior.to_dict()
        entry["hypothesis_id"] = hypothesis.id
        priors.append(entry)
    return priors


def _remember_outcome(state: InvestigationState, ctx: RunContext) -> None:
    """把本次结论记进记忆：断言为根因的记一次命中，被推翻的记一次反驳。

    为什么两个都记：只记命中会让记忆变成"永远在说同一个名字"的机器。
    反驳也是信息 —— 一个反复被推翻的候选，下次不该再因为"老熟人"加分。

    为什么记的是**排序第一名**而不是所有 SUPPORTED：并列时两个都
    SUPPORTED，把它们都记成命中等于把不确定性也写成了经验。
    """
    if ctx.memory is None:
        return
    ranked = state.get("ranked") or []
    hypotheses = state.get("hypotheses") or []
    if not ranked:
        return
    from traceops.domain.models import HypothesisStatus

    by_id = {h.id: h for h in hypotheses}
    top = ranked[0]
    lead = by_id.get(top.hypothesis_id)
    try:
        if lead is not None and top.status is HypothesisStatus.SUPPORTED:
            ctx.memory.record(
                service=lead.candidate_service,
                fault_type=lead.fault_type or "",
                component=lead.candidate_component,
            )
        for candidate in ranked:
            if candidate.status is not HypothesisStatus.REFUTED:
                continue
            loser = by_id.get(candidate.hypothesis_id)
            if loser is None:
                continue
            ctx.memory.record(
                service=loser.candidate_service,
                fault_type=loser.fault_type or "",
                component=loser.candidate_component,
                refuted=True,
            )
    except (sqlite3.Error, OSError, ValueError) as exc:
        # 记不住下一次调查，不该让这一次的报告丢掉；但编程错误不属于这一类。
        log.warning("incident memory could not record the outcome: %s", exc, exc_info=True)


def _build_gateway(ctx: RunContext) -> ToolGateway:
    gateway = ToolGateway(cache=ctx.cache)
    register_builtin_tools(gateway, ctx.adapters, _active_manifest(ctx))
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
    builder.add_node("discriminate", _discriminate_node)
    builder.add_node("report", _report_node)

    builder.add_edge(START, "plan")
    builder.add_edge("plan", "collect")
    builder.add_conditional_edges(
        "collect", _route_after_collect, {"stop": END, "continue": "hypothesize"}
    )
    builder.add_edge("hypothesize", "link")
    builder.add_edge("link", "correlate")
    builder.add_edge("correlate", "rank")
    # 唯一的回路：rank 说不清"这两个里是哪个"时，再取一趟证据回来重排。
    # 出口由 rank 里的预算判断决定（`can_discriminate`），所以这个环有界。
    builder.add_conditional_edges(
        "rank", _route_after_rank, {"discriminate": "discriminate", "report": "report"}
    )
    builder.add_edge("discriminate", "collect")
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
        "no_data_queries": [],
        "degraded_reads": [],
        "failed_sources": [],
        "cache_hits": [],
        # 判别回路的状态：走第几趟、当前阶段、已经问过哪些（防重复付费）。
        "round": 0,
        "phase": "probe",
        "discriminated": [],
        "can_discriminate": False,
        "next_plan": [],
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
    session = _open_session(ctx, request.investigation_run_id)
    try:
        return _run_recording(request, ctx, session, thread_id=thread_id,
                              checkpointer=checkpointer, on_node=on_node)
    finally:
        _close_session(session)


def _open_session(ctx: RunContext, run_id: str) -> Any:
    """把 `ctx.session` 变成一份真的会话记录（目录或已构造的 recorder 都接受）。

    允许传目录而不是文件：run id 只有在 request 里才有，而调用方（worker CLI）
    在构造 RunContext 时还不知道它 —— 让它去猜文件名只会制造两套命名规则。
    """
    from traceops.runtime.session import SessionRecorder, recorder_for

    session = getattr(ctx, "session", None)
    if session is None or isinstance(session, SessionRecorder):
        if session is not None:
            session.run_id = session.run_id or run_id
            session.__enter__()
        return session
    recorder = recorder_for(run_id, directory=session)
    ctx.session = recorder
    recorder.__enter__()
    return recorder


def _close_session(session: Any) -> None:
    if session is not None and getattr(session, "enabled", False):
        session.__exit__(None, None, None)


def _run_recording(request: InvestigationRequest, ctx: RunContext, session: Any, *,
                   thread_id: str | None, checkpointer: Any | None, on_node: Any) -> InvestigationResult:
    if session is not None:
        _session_record(
            ctx, "investigation.start",
            incident_id=request.incident_id,
            service_hint=request.service_hint,
            window_start=request.window_start.isoformat(),
            window_end=request.window_end.isoformat(),
            priority=getattr(request, "priority", None),
            alert_summary_present=getattr(request, "alert_summary", None) is not None,
            max_tool_calls=ctx.budget.max_tool_calls,
        )
    result = _run_graph(request, ctx, thread_id=thread_id, checkpointer=checkpointer, on_node=on_node)
    if session is not None:
        report = result.report
        _session_record(
            ctx, "investigation.end",
            status=result.status,
            diagnosis=report.diagnosis_status if report is not None else None,
            evidence=len(report.evidence) if report is not None else 0,
            tool_calls=result.tool_calls,
            tokens_used=result.tokens_used,
            # +1：这一行本身还没写出去（`record` 先取字段再落盘），
            # 报的数必须等于文件里的行数，否则对不上就会被当成"丢了日志"。
            events_written=getattr(session, "written", 0) + 1,
            session_ref=_session_ref(ctx),
        )
    return result


def _run_graph(request: InvestigationRequest, ctx: RunContext, *, thread_id: str | None,
               checkpointer: Any | None, on_node: Any) -> InvestigationResult:
    # 在**跑图之前**记一次策略：报告随后会带上"这一轮相对上一轮改了什么"。
    # 放在图里的话，每个节点都会各自观察一次，同一轮会得到互相矛盾的结论。
    ctx.observe_strategy()
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
        no_data_queries=list(final.get("no_data_queries", [])),
    )
