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

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetExhausted, BudgetManager
from traceops.runtime.deadletter import (
    DeadLetterRecord,
    FailureCategory,
    classify_failure,
    now_iso,
)
from traceops.runtime.retry import RetryPolicy, RetryTracker, is_retryable
from traceops.evidence.audit import AuditCenter, AuditKind
from traceops.runtime.run_ledger import RunLedger
from traceops.runtime.investigation_run import InvestigationConfigSnapshot, InvestigationRun, RunStatus

SCHEMA_VERSION = "1.0"


# ---------------- 事件端口 ----------------

class EventSource(Protocol):
    """消费 investigation.requested ��返回原始 JSON 消息列表。"""

    def poll(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

    def ack(self, message_id: str) -> None: ...


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
        retry_policy: RetryPolicy | None = None,
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
        # 重试策略：按 failure_reason 分类，不盲目重试（见 runtime/retry.py）
        self.retry_policy = retry_policy or RetryPolicy()

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

    def _retry_decision(self, reason: str):
        """独立的记账器：一次失败 → 一个决策。放在这里而不是 Run 上，
        因为单次 poll_once 就是一次尝试。"""
        tracker = RetryTracker(policy=self.retry_policy)
        tracker.record_failure(reason=reason)
        return tracker.decide(reason=reason)

    def poll_once(self, *, limit: int = 50) -> WorkerStats:
        stats = WorkerStats()
        batch = self.source.poll(limit=limit)
        for message in batch:
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
                stats.skipped_duplicate += 1
                self.source.ack(message["id"])
                continue
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

    def _handle(self, message: dict[str, Any], stats: WorkerStats) -> None:
        try:
            request = self.derive_request(message)
        except Exception as exc:  # 请求本身非法 → 不重试
            self._dead_letter(message, exc)
            self.sink.publish(_invalid_request_event(message, str(exc)))
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
        )
        run = InvestigationRun.open(incident_id=request.incident_id,
                                    run_id=request.investigation_run_id,
                                    config=config_snapshot)
        run.queue()
        run.start(node="plan")
        self.sink.publish(started_event(run))
        stats.run_ids.append(run.run_id)
        # 进度上报：每个节点完成时发一条 investigation.updated。
        # 此前整个调查只发 started_event 一条，前端只能看到 "RUNNING" ——
        # 跑了 30 秒的调查没有中间状态，SSE 也就无从展示。
        step_counter = {"index": 0}

        def _on_node(node_name, state):
            index = step_counter["index"]
            step_counter["index"] += 1
            evidence_count = len(state.get("evidence") or [])
            self.sink.publish(progress_event(
                run, node=node_name, step_index=index,
                description=None, evidence_count=evidence_count))

        try:
            result = run_investigation(request, ctx, on_node=_on_node)
            run.record_usage(tool_calls=result.tool_calls, tokens=result.tokens_used,
                             rounds=ctx.budget.rounds)
            if result.status == "FAILED":
                reason = _classify_failure(result.errors)
                run.fail(reason=reason, node="collect")
                # 是否可重试由统一策略决定，不在各处零散判断
                self.ledger.finish(run.run_id, outcome=f"FAILED:{reason}")
                self.sink.publish(failed_event(run, reason=reason,
                                               retryable=is_retryable(reason),
                                               detail="; ".join(result.errors)))
                stats.failed += 1
                return
            run.complete()
            self.ledger.finish(run.run_id, outcome="COMPLETED")
            self.sink.publish(completed_event(run, result.report))
            stats.succeeded += 1
        except BudgetExhausted as exc:
            run.fail(reason=FAILURE_BUDGET, node="collect")
            self.ledger.finish(run.run_id, outcome=f"FAILED:{FAILURE_BUDGET}")
            self.sink.publish(failed_event(run, reason=FAILURE_BUDGET,
                                           retryable=is_retryable(FAILURE_BUDGET),
                                           detail=str(exc)))
            stats.failed += 1
        except Exception as exc:  # 未预期错误：保留 checkpoint 供恢复
            run.fail(reason=FAILURE_INTERNAL, node=run.current_node)
            self.ledger.finish(run.run_id, outcome=f"FAILED:{FAILURE_INTERNAL}")
            self.sink.publish(failed_event(run, reason=FAILURE_INTERNAL,
                                           retryable=is_retryable(FAILURE_INTERNAL),
                                           detail=repr(exc)))
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


def derive_request_from_event(message: dict[str, Any]) -> InvestigationRequest:
    """把 investigation.requested 的 payload 转成调查请求。

    契约里 time_range 是 [start, end]；fault_start 取窗口末尾（告警触发时刻），
    这是 P0 的合理近似 —— 精确故障起点需要真实告警时间戳，属 S4 待验证项。
    """
    envelope, payload = _normalise_message_shape(message)

    if envelope.get("event_type") not in (None, "investigation.requested"):
        raise ValueError(f"unexpected event_type: {envelope.get('event_type')!r}")
    if envelope.get("producer") not in (None, "control-plane"):
        raise ValueError(f"unexpected producer: {envelope.get('producer')!r}")

    required = ("investigation_run_id", "incident_id", "time_range", "service_hint")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"payload missing required fields: {missing}")

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
    )


def dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, sort_keys=True)
