"""Worker：消费 investigation.requested，跑完调查，产出终态事件。

这是**闭环的关键连接点**：没有它，Java 发的事件永远不会被执行，
调查结果也永远回不到 Control Plane。

设计要点：
- 消息只传 ID/窗口/配置版本（蓝图 §4.3）；完整证据不进消息体
- 幂等：按 investigation_run_id 去重，重复投递直接 ACK 跳过
- handler 是纯函数式接口：测试用 FakeEventSource/FakeSink，生产接 Redis Streams
- 失败必须产出 investigation.failed，且带可聚合的枚举原因
"""
from __future__ import annotations

import logging

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Protocol, runtime_checkable

from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetExhausted, BudgetManager
from traceops.runtime.deadletter import (
    DeadLetterRecord,
    FailureCategory,
    classify_failure,
    now_iso,
)
from traceops.runtime.retry import is_retryable
from traceops.runtime.event_contract import validate_event
from traceops.evidence.audit import AuditCenter, AuditKind
from traceops.runtime.run_ledger import RunLedger
from traceops.runtime.investigation_run import InvestigationConfigSnapshot, InvestigationRun, RunStatus

SCHEMA_VERSION = "1.0"


# ---------------- 事件端口 ----------------

class EventSource(Protocol):
    """消费 investigation.requested ��返回原始 JSON 消息列表。"""

    def poll(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

    def ack(self, message_id: str) -> None: ...


@runtime_checkable
class EventSink(Protocol):
    """发布 investigation.* 结果事件。"""

    def publish(self, message: dict[str, Any]) -> None: ...


class InMemoryEventSource:
    """测试用：一个可编程的消息队列。"""

    def __init__(self) -> None:
        self._messages: list[tuple[str, dict[str, Any]]] = []
        self.acked: list[str] = []

    def push(self, message: dict[str, Any], message_id: str | None = None) -> str:
        mid = message_id or f"msg-{uuid.uuid4()}"
        self._messages.append((mid, message))
        return mid

    def poll(self, *, limit: int = 50) -> list[dict[str, Any]]:
        batch = self._messages[:limit]
        self._messages = self._messages[limit:]
        return [{"id": mid, **message} for mid, message in batch]

    def ack(self, message_id: str) -> None:
        self.acked.append(message_id)


class InMemoryEventSink:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def publish(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


# ---------------- 事件构造 ----------------

def _envelope(event_type: str, payload: dict[str, Any], *, run_id: str, incident_id: str,
              version: int = 1) -> dict[str, Any]:
    return {
        "envelope": {
            "event_id": f"evt-{uuid.uuid4()}",
            "event_type": event_type,
            "schema_version": SCHEMA_VERSION,
            "occurred_at": datetime.now(UTC).isoformat(),
            "correlation_id": incident_id,
            "causation_id": None,
            "aggregate_id": run_id,
            "aggregate_version": version,
            "producer": "agent-runtime",
        },
        "payload": payload,
    }


log = logging.getLogger(__name__)


def started_event(run: InvestigationRun) -> dict[str, Any]:
    return _envelope(
        "investigation.updated",
        {
            "investigation_run_id": run.run_id,
            "status": "RUNNING",
            "aggregate_version": run.aggregate_version,
            "current_node": run.current_node or "plan",
        },
        run_id=run.run_id,
        incident_id=run.incident_id,
        version=run.aggregate_version,
    )


#: 节点 → 人可读的进度描述。SSE 前端直接展示，不做二次映射。
_NODE_DESCRIPTIONS: dict[str, str] = {
    "plan": "planning the investigation scope",
    "collect": "collecting evidence from datasources",
    "hypothesize": "generating root cause candidates",
    "link": "linking evidence to hypotheses",
    "correlate": "correlating timeline and propagation",
    "rank": "ranking candidate root causes",
    "report": "assembling the diagnosis report",
}


def progress_event(run: InvestigationRun, *, node: str, step_index: int,
                   description: str | None = None,
                   evidence_count: int = 0) -> dict[str, Any]:
    """一条**节点级**进度事件（给 SSE 用）。

    为什么需要：`contracts/` 里 `investigation-updated` 早就声明了
    `progress.{step_index, step_description, evidence_count}`，
    但两端从未填充过，且整个调查只发一条 `started_event`。
    于是跑了 30 秒的调查在前端只有一个 "RUNNING" —— 进度推送**缺的不是传输，是数据**。

    `step_index` 必须单调递增：SSE 客户端可能乱序收到消息（重连/多副本），
    靠它才能重排。不能复用 `aggregate_version` —— 那个字段同时被终态事件消耗，
    两类事件挤在同一序列里会让客户端无法判断"这是进度还是结论"。
    """
    return _envelope(
        "investigation.updated",
        {
            "investigation_run_id": run.run_id,
            "status": "RUNNING",
            "aggregate_version": run.aggregate_version,
            "current_node": node,
            "progress": {
                "step_index": int(step_index),
                "step_description": description or _NODE_DESCRIPTIONS.get(node, node),
                "evidence_count": int(evidence_count),
            },
        },
        run_id=run.run_id,
        incident_id=run.incident_id,
        version=run.aggregate_version,
    )


def completed_event(run: InvestigationRun, report) -> dict[str, Any]:
    """终态事件：只带摘要与计数，完整报告走 rca_report_ref（蓝图 §4.3 大对象不进消息体）。"""
    candidates = [
        {
            "candidate_service": h.candidate_service,
            "candidate_component": h.candidate_component,
            "fault_type": h.fault_type,
            "rank": h.rank or 1,
            "hypothesis_status": h.status.value,
        }
        for h in (report.hypotheses if report else [])
    ][:5]
    summary = {"total": len(report.evidence) if report else 0}
    for evidence in (report.evidence if report else []):
        summary[evidence.evidence_type.value + "s"] = summary.get(evidence.evidence_type.value + "s", 0) + 1

    payload = {
        "investigation_run_id": run.run_id,
        "diagnosis_status": report.diagnosis_status if report else "INCONCLUSIVE",
        "rca_report_ref": f"{run.run_id}/report",
        "root_cause_candidates": candidates,
        "evidence_summary": summary,
        "usage": {
            "tool_calls": run.tool_calls,
            "tokens": run.tokens_used,
            "rounds": run.rounds,
            "duration_seconds": int((run.updated_at - run.created_at).total_seconds()),
        },
        "abstained": bool(report.abstained) if report else True,
        # 必须进消息体：控制面靠它决定下一轮重试先查哪个源，而
        # rca_report_ref 指向的存储控制面并不读。空列表是有效信息
        # （这一轮所有源都够得到），不是"没填"。
        "failed_sources": list(
            (report.run_snapshot or {}).get("failed_sources", []) if report else []
        ),
    }
    return _envelope("investigation.completed", payload,
                     run_id=run.run_id, incident_id=run.incident_id, version=run.aggregate_version)


# failure_reason 必须是枚举 —— 自由文本无法聚合统计（contracts 里有同样的约束）
FAILURE_BUDGET = "BUDGET_EXHAUSTED"
FAILURE_INVALID = "INVALID_REQUEST"
FAILURE_INTERNAL = "INTERNAL_ERROR"


def failed_event(run: InvestigationRun, *, reason: str, retryable: bool,
                 detail: str | None = None) -> dict[str, Any]:
    payload = {
        "investigation_run_id": run.run_id,
        "terminal_status": run.status.value,
        "failure_reason": reason,
        "retryable": retryable,
        "failed_node": run.current_node,
        "checkpoint_id": run.checkpoint_id,
        "usage_at_failure": {
            "tool_calls": run.tool_calls,
            "tokens": run.tokens_used,
            "rounds": run.rounds,
        },
    }
    if detail:
        payload["error_detail"] = detail[:2000]
    return _envelope("investigation.failed", payload,
                     run_id=run.run_id, incident_id=run.incident_id, version=run.aggregate_version)


# ---------------- worker ----------------

DeriveRequest = Callable[[dict[str, Any]], InvestigationRequest]
BuildContext = Callable[[InvestigationRequest], RunContext]


@dataclass
class WorkerStats:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_duplicate: int = 0
    #: 接管了**陈旧认领**（持有者大概率已死）的消息数。
    #: 与 skipped_duplicate 分开计数：一个是"别人在做，让给他"，
    #: 一个是"他死了，我接手" —— 运维需要区分这两件事。
    reclaimed: int = 0
    #: **结果事件没能发出去**的次数。
    #: 这是一个必须存在的计数：调查本身可能成功，但结论没进 Redis，
    #: 控制面那边看到的就是"这件事没发生过"。不单独计数的话，
    #: 运维看到的只有"处理了 N 条"，看不出其中 N 条的结论丢了。
    publish_failed: int = 0
    run_ids: list[str] = field(default_factory=list)


class InvestigationWorker:
    def __init__(
        self,
        *,
        source: EventSource,
        sink: EventSink,
        derive_request: DeriveRequest,
        build_context: BuildContext,
        known_run_ids: set[str] | None = None,
        dead_letters: Any | None = None,
        stale_claim_seconds: float = 900.0,
        ledger: RunLedger | None = None,
    ) -> None:
        self.source = source
        self.sink = sink
        self.derive_request = derive_request
        self.build_context = build_context
        # 账本优先：跨重启有效的去重。known_run_ids 保留作向后兼容的预置集合。
        self.ledger = ledger if ledger is not None else RunLedger()
        self._known_run_ids = set(known_run_ids or ())
        # 坏消息不能静默 ACK 掉：转移到死信流，保留审计痕迹
        self.dead_letters = dead_letters
        # **重试不在这里决策**：worker 只在失败事件里说清"这类失败重试
        # 有没有意义"（is_retryable），次数、退避和何时放弃由控制面决定
        # —— 事故状态机和持久化 outbox 都在那边，而这里的计数活不过重启。
        # 认领多久算"陈旧"。默认与 BudgetManager 的 deadline 一致（900s）：
        # 一个调查最长跑这么久，超过它还没结束，说明持有者大概率已经死了，
        # 此时接管是安全的；小于它则会**误抢**正在正常处理的消息。
        self.stale_claim_seconds = stale_claim_seconds

    def audit_timeline(self, *, gateway=None, kind=None, subject=None,
                       since=None, until=None):
        """合并三处审计产出统一时间线（按时间正序）。

        Args:
            gateway: 可选的 `ToolGateway`。工具审计是内存列表、且属于某次调查，
                所以不能由 worker 自己持有 —— 调用方传进来。
            kind/subject/since/until: 透传给 `AuditCenter.timeline`。

        为什么把 gateway 做成参数而不是构造参数：同一个 worker 会处理很多 run，
        每个 run 有自己的 gateway；绑在 worker 上会混淆"哪个工具调用属于哪次调查"。
        """
        center = AuditCenter()
        center.add_all(AuditCenter.from_ledger_rows(self.ledger.rows()))
        if gateway is not None:
            center.add_all(AuditCenter.from_gateway_records(gateway.audit_log()))
        if self.dead_letters is not None:
            for record in getattr(self.dead_letters, "records", []) or []:
                center.add(AuditCenter.from_dead_letter(record))
        return center.timeline(kind=kind, subject=subject, since=since, until=until)

    def poll_once(self, *, limit: int = 50) -> WorkerStats:
        stats = WorkerStats()
        batch = self.source.poll(limit=limit)
        for message in batch:
            # **类型闸门在第一位**：worker 只对 investigation.requested 有反应。
            #
            # 为什么必须放在最前面：控制面和 worker 读的是**同一条**流
            # traceops.events（不同消费组），worker 自己也会往这条流写
            # investigation.updated / completed / failed。所以 worker 的
            # 消费组一定会读到**自己写的消息**。
            #
            # 此前这类消息会被当成"非法请求"：进 dead_letter，并**再发一条
            # investigation.failed** —— 每条自己写的消息都变成一条新的失败
            # 事件，形成自我放大的回路（实测：跑一次 worker，流里多出两条
            # failed，dead_letter 表被自己的输出污染）。
            #
            # 注意区分：**不关心的事件**（自己写的）跳过并 ACK；
            # **关心但坏了的事件**（真请求缺字段）仍然进 dead_letter。
            if not _is_requested_event(message):
                stats.skipped_duplicate += 1
                self.source.ack(message["id"])
                continue

            run_id = _extract_run_id(message)
            # **认领**而不是"检查一下有没有见过"。
            # 旧写法 is_seen(...) 与后面的 record(...) 之间隔着整个调查时长，
            # 并发 worker（Redis XAUTOCLAIM 抢回 pending 消息时必然发生）
            # 会同时通过检查，各自跑一遍同一 run —— 重复烧工具配额、重复发结论。
            # claim 把"检查+占用"合成一步：只有一个 worker 能成功。
            # 进程内已知集合先挡一层（构造时预置的 run 视为已处理）。
            # 它是账本的**快速路径**，但不能替代 claim：集合只活在进程内。
            if run_id is not None and run_id in self._known_run_ids:
                stats.skipped_duplicate += 1
                self.source.ack(message["id"])
                continue
            if run_id is not None and not self.ledger.claim(run_id):
                # 认领失败有**三种**含义，必须区分 —— 这是三种不同的动作：
                #
                # 1. 已经跑完（DONE）→ **必须 ACK**。
                #    这个 run 不需要再处理了，消息留在 PEL 里只会被
                #    XAUTOCLAIM 无限重投（worker 每次跳过、每次不 ACK）。
                #    实测：entry 归属别的 consumer 时 XACK 根本不生效，
                #    消息就永久钉在 PEL 里，`pending` 只增不减。
                #
                # 2. 另一个 worker 正在处理（IN_PROGRESS + 新鲜）→ **不能 ACK**。
                #    "别人正在做"不等于"已经做完了"。ACK 掉之后若持有者崩溃，
                #    这条消息再也没有任何 worker 会处理 —— 事故永久停摆。
                #    留在 PEL 里，持有者完成时会自己 ACK，或它死掉后被别人接管。
                #
                # 3. 持有者大概已死（IN_PROGRESS + 陈旧）→ 接管并执行。
                state = self.ledger.claim_state(run_id)
                if state == "DONE":
                    stats.skipped_duplicate += 1
                    self.source.ack(message["id"])
                    continue
                if not self.ledger.reclaimable(run_id, older_than_seconds=self.stale_claim_seconds):
                    # **不 ACK**：持有者还在跑。ACK 会把它从 PEL 里移除，
                    # 而持有者一旦崩溃，这条消息就再没有任何 worker 会处理。
                    stats.skipped_duplicate += 1
                    continue
                # 陈旧认领（持有者大概率已死）→ 接管：重新认领并继续执行
                self.ledger.reclaim(run_id)
                stats.reclaimed += 1
            if run_id is not None:
                self._known_run_ids.add(run_id)
            self._handle(message, stats)
            self.source.ack(message["id"])
            stats.processed += 1
        return stats

    def _dead_letter(self, message: dict[str, Any], exc: Exception) -> None:
        if self.dead_letters is None:
            return
        self.dead_letters.send(DeadLetterRecord(
            original_entry_id=str(message.get("id", "unknown")),
            stream=str(message.get("_stream", "traceops.events")),
            group=str(message.get("_group", "traceops-workers")),
            consumer=str(message.get("_consumer", "worker")),
            category=classify_failure(str(exc)),
            reason=str(exc)[:2000],
            raw=str(message)[: DeadLetterRecord.MAX_RAW_CHARS],
            occurred_at=now_iso(),
        ))

    def _publish(self, message: dict[str, Any], stats: WorkerStats,
                 *, event_type: str = "unknown") -> bool:
        """发一条结果事件；发不出去就**记账并继续**，不让 worker 死掉。

        为什么不能让异常直接冒出去：publish 失败通常是下游的问题
        （Redis 抖了一下、网络断了）。如果它能把整个 worker 带走，
        那么一个下游抖动就会让**所有**在途调查中断 —— 故障被放大成停机。
        调查本身已经做完，把结论丢掉很可惜，但让 worker 活着更重要：
        它可以继续跑完下一批。

        但"丢掉"必须**可见**：stats.publish_failed 会进 poll 统计，
        日志里也有 error 级记录。静默丢弃的结果是控制面那边永远等不到结论，
        而 worker 这边一切正常 —— 这种故障最难查。

        返回是否发布成功，让调用方能据此判断要不要补救。
        """
        try:
            self.sink.publish(message)
            return True
        except Exception as exc:
            stats.publish_failed += 1
            log.error("failed to publish result event",
                      extra={"event_type": event_type,
                             "error_type": type(exc).__name__,
                             "reason": str(exc)[:500]},
                      exc_info=True)
            return False

    def _handle(self, message: dict[str, Any], stats: WorkerStats) -> None:
        try:
            request = self.derive_request(message)
        except Exception as exc:  # 请求本身非法 → 不重试
            # 结构化字段（extra=）而不是拼进 message：拼进去就再也无法
            # 按 message_id / 异常类型过滤和聚合，而那正是排障第一步。
            log.warning("invalid investigation request",
                        extra={"message_id": str(message.get("id", "")),
                               "error_type": type(exc).__name__,
                               "reason": str(exc)[:200]})
            self._dead_letter(message, exc)
            self._publish(_invalid_request_event(message, str(exc)), stats,
                          event_type="investigation.invalid")
            stats.failed += 1
            return

        # **只构造一次**：旧写法先调 build_context 只为读一个 max_tool_calls，
        # 然后把整个 RunContext 丢掉再造一个。真实实现里那意味着连两次数据源、
        # 开两个 SQLite 连接，而且第一次的 Budget 上限可能被静默丢弃。
        #
        # 现在提前到建 run 之前：配置快照要**从实际运行配置派生**，
        # 否则快照永远是默认值（接了模型却记成 deterministic-rules）。
        ctx = self.build_context(request)
        budget = BudgetManager(
            max_tool_calls=getattr(ctx.budget, "max_tool_calls", 50),
        )

        model_client = getattr(ctx, "model", None)
        config_snapshot = InvestigationConfigSnapshot.from_runtime(
            model_name=getattr(model_client, "name", None) if model_client else None,
            scope_depth=getattr(ctx, "scope_depth", 1),
            max_tool_calls=getattr(ctx.budget, "max_tool_calls", 50),
            config_version=getattr(request, "config_version", None),
            # 策略指纹从 ctx 自己派生 —— 预算与深度进 run_snapshot，
            # 阈值/权重/闸门/提示词进 digest。两者的差别决定了事后能问出
            # "是策略变了还是数据变了"，所以不能混在一起。
            strategy_digest=ctx.strategy_digest(),
            run_snapshot=ctx.run_snapshot(request),
        )
        run = InvestigationRun.open(incident_id=request.incident_id,
                                    run_id=request.investigation_run_id,
                                    config=config_snapshot)
        run.queue()
        run.start(node="plan")
        self._publish(started_event(run), stats, event_type="investigation.started")
        stats.run_ids.append(run.run_id)
        # 进度上报：每个节点完成时发一条 investigation.updated。
        # 此前整个调查只发 started_event 一条，前端只能看到 "RUNNING" ——
        # 跑了 30 秒的调查没有中间状态，SSE 也就无从展示。
        step_counter = {"index": 0}

        def _on_node(node_name, state):
            index = step_counter["index"]
            step_counter["index"] += 1
            evidence_count = len(state.get("evidence") or [])
            self._publish(progress_event(
                run, node=node_name, step_index=index,
                description=None, evidence_count=evidence_count), stats,
                event_type="investigation.updated")

        try:
            result = run_investigation(request, ctx, on_node=_on_node)
            run.record_usage(tool_calls=result.tool_calls, tokens=result.tokens_used,
                             rounds=ctx.budget.rounds)
            if result.status == "FAILED":
                reason = _classify_failure(result.errors)
                run.fail(reason=reason, node="collect")
                # 是否可重试由统一策略决定，不在各处零散判断
                self.ledger.finish(run.run_id, outcome=f"FAILED:{reason}")
                self._publish(failed_event(run, reason=reason,
                                           retryable=is_retryable(reason),
                                           detail="; ".join(result.errors)), stats,
                              event_type="investigation.failed")
                # 失败事件会进 Redis/控制面，但那是**下游**看得到的东西；
                # worker 自己的日志必须也有一份 —— 否则从 worker 主机上
                # 排查时只能看到"少了一条消息"。
                log.error("investigation failed",
                          extra={"message_id": message.get("id"),
                                 "run_id": run.run_id,
                                 "incident_id": request.incident_id,
                                 "failure_reason": reason,
                                 "errors": "; ".join(result.errors)})
                stats.failed += 1
                return
            run.complete()
            self.ledger.finish(run.run_id, outcome="COMPLETED")
            self._publish(completed_event(run, result.report), stats,
                          event_type="investigation.completed")
            stats.succeeded += 1
        except BudgetExhausted as exc:
            log.warning("investigation budget exhausted",
                        extra={"message_id": message.get("id"),
                               "run_id": run.run_id,
                               "reason": str(exc)})
            run.fail(reason=FAILURE_BUDGET, node="collect")
            self.ledger.finish(run.run_id, outcome=f"FAILED:{FAILURE_BUDGET}")
            self._publish(failed_event(run, reason=FAILURE_BUDGET,
                                       retryable=is_retryable(FAILURE_BUDGET),
                                       detail=str(exc)), stats,
                          event_type="investigation.failed")
            stats.failed += 1
        except Exception as exc:  # 未预期错误：保留 checkpoint 供恢复
            log.error("investigation raised an unexpected error",
                      extra={"message_id": message.get("id"),
                             "run_id": run.run_id,
                             "error_type": type(exc).__name__,
                             "reason": str(exc)},
                      exc_info=True)
            run.fail(reason=FAILURE_INTERNAL, node=run.current_node)
            self.ledger.finish(run.run_id, outcome=f"FAILED:{FAILURE_INTERNAL}")
            self._publish(failed_event(run, reason=FAILURE_INTERNAL,
                                       retryable=is_retryable(FAILURE_INTERNAL),
                                       detail=repr(exc)), stats,
                          event_type="investigation.failed")
            stats.failed += 1


def _extract_run_id(message: dict[str, Any]) -> str | None:
    """取 investigation_run_id，两种消息形状都要支持。

    摊平形状（Redis entry）下 message["payload"] 是整份文档，
    run_id 在更深一层；只看第一层会让幂等完全失效（重复消息被当新消息处理）。
    """
    _, payload = _normalise_message_shape(message)
    if isinstance(payload, dict):
        run_id = payload.get("investigation_run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    return None


def _classify_failure(errors: list[str]) -> str:
    joined = " ".join(errors).lower()
    if "budget exhausted" in joined or "tool call budget" in joined:
        return FAILURE_BUDGET
    if "invalid" in joined:
        return FAILURE_INVALID
    return FAILURE_INTERNAL


def _invalid_request_event(message: dict[str, Any], detail: str) -> dict[str, Any]:
    payload = message.get("payload") or {}
    return _envelope(
        "investigation.failed",
        {
            "investigation_run_id": _extract_run_id(message) or "unknown",
            "terminal_status": "FAILED",
            "failure_reason": FAILURE_INVALID,
            "retryable": False,
            "error_detail": detail[:2000],
        },
        run_id=_extract_run_id(message) or "unknown",
        incident_id=payload.get("incident_id", "unknown"),
    )


# ---------------- 默认派生器 ----------------

def _normalise_message_shape(message: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """统一两种合法输入形状。

    A) 契约文档形状（推荐）：{"envelope": {...}, "payload": {...}}
    B) Redis Streams 摊平形状：{"payload": {"envelope": {...}, "payload": {...}}, "id": ...}
       —— entry 里只有一个 payload field 装着整份文档（生产者通常这么写）

    两种都必须支持：形状差异不应该让调用方"记得包 adapter"才能正常工作，
    那是隐藏的易错点。这里在入口处归一到契约形状。
    """
    outer = message.get("payload") or {}
    if isinstance(outer, dict) and "envelope" in outer and "payload" in outer:
        # B 形状：外层 payload 实际是整份文档
        return outer.get("envelope") or {}, outer.get("payload") or {}
    return message.get("envelope") or {}, outer


#: worker 唯一关心的入站事件类型。
#:
#: 用常量而不是字面量，是因为这个值同时出现在三处（类型闸门、
#: derive 校验、文档），字面量复制粘贴迟早会漂。
REQUESTED_EVENT_TYPE = "investigation.requested"

#: **worker 自己会写出去**的事件类型：它们出现在同一条流上，
#: 但不是给 worker 的输入。
#:
#: 闸门只跳过这些"认得出但不管"的事件；类型缺失或未知的消息
#: 仍然交给 derive 判非法 → 进 dead_letter。跳过范围太宽的后果是
#: 毒消息被静默吞掉 —— 看不见的坏消息比看得见的坏消息危险得多。
_OWN_OUTPUT_EVENT_TYPES = frozenset({
    "investigation.updated",
    "investigation.completed",
    "investigation.failed",
    "investigation.started",
    "investigation.invalid",
})


def _is_requested_event(message: dict[str, Any]) -> bool:
    """这条消息是不是 worker 该处理的入站请求？

    三种情况：
    - ``event_type == investigation.requested`` → 处理（合法性交给 derive 判）
    - ``event_type`` 是 worker 自己写的那种 → 跳过（但必须 ACK，见 poll_once）
    - ``event_type`` 缺失或未知 → 也处理，交给 derive 判非法并进 dead_letter
    """
    envelope, _payload = _normalise_message_shape(message)
    event_type = envelope.get("event_type")
    return event_type not in _OWN_OUTPUT_EVENT_TYPES


def derive_request_from_event(message: dict[str, Any]) -> InvestigationRequest:
    """把 investigation.requested 的 payload 转成调查请求。

    契约里 time_range 是 [start, end]；fault_start 取窗口末尾（告警触发时刻），
    这是 P0 的合理近似 —— 精确故障起点需要真实告警时间戳，属 S4 待验证项。
    """
    envelope, payload = _normalise_message_shape(message)

    # 契约是唯一事实来源。这里原来手写了一份 required 列表和两条枚举检查，
    # 那是 schema 的第二份副本 —— 副本一定会漂移，而且漂移后无法判断谁对。
    # 真实案例：Java 侧发出 priority="manual"，两侧手写检查都没有 priority 这一项，
    # 于是坏消息一路走到 payload.get("priority", "normal")，取了个没人认识的值。
    # 现在校验的是 contracts/ 里那份文件本身。
    validate_event(REQUESTED_EVENT_TYPE, {"envelope": envelope, "payload": payload})

    window = payload["time_range"]
    return InvestigationRequest(
        investigation_run_id=payload["investigation_run_id"],
        incident_id=payload["incident_id"],
        environment=payload.get("environment", "demo"),
        service_hint=payload["service_hint"],
        fault_start=window["end"],
        window_start=window["start"],
        window_end=window["end"],
        config_change=payload.get("config_change"),
        priority=payload.get("priority", "normal"),
        # 告警原文：规则读不到、只有模型能用的那一维信息。
        # 契约里加了这个字段却没在这里读，等于整条链路白改 ——
        # 这正是它之前的状态：字段在模型请求里有、在生产路径上永远是 None。
        alert_summary=payload.get("alert_summary"),
        budget=payload.get("budget"),
        scope_depth=payload.get("scope_depth"),
        topology_edges=payload.get("topology_edges"),
        # 重试上下文：在重做哪一轮、上一轮哪些源够不到。
        # 前一轮缺什么直接决定这一轮的**收集顺序**（见 _prioritise_plan）——
        # 不读它的话，重试只是在同一次失败上再花一遍预算。
        retry_of=payload.get("retry_of"),
        known_unavailable=payload.get("known_unavailable"),
        # 变更清单：一次发版往往同时动配置、同时扩缩容。只收单条 config_change
        # 会逼调用方挑一条，而被丢掉的那条可能才是原因（见 _change_items）。
        changes=payload.get("changes"),
    )


def dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, sort_keys=True)
