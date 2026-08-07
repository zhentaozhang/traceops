"""Checkpoint 恢复测试（蓝图 §8.4 / P2）。

场景：调查跑到一半进程崩了。重启后必须能从断点继续，而不是从头再查一遍
（重复查询会二次消耗预算、且可能查到不同的数据，破坏可复现性）。

用 LangGraph 的 MemorySaver（不引入 SqliteSaver 依赖）。生产换持久化后端时
checkpointer 实现替换即可，接口不变。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest,
    RunContext,
    build_investigation_graph,
    run_investigation,
)
from traceops.runtime.budget import BudgetManager

FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 30, 10, 6, 0, tzinfo=UTC)


class CountingAdapters:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.error_rate_calls = 0
        self.fail_at = fail_at

    def query_service_error_rate(self, *, service, start, end):
        self.error_rate_calls += 1
        if self.fail_at is not None and self.error_rate_calls >= self.fail_at:
            raise RuntimeError("simulated crash during collection")
        ts = int(FAULT_START.timestamp())
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": service}, "values": [[ts - 60, "0.01"], [ts, "0.4"]]}]}}

    def query_service_logs(self, *, service, start, end, filter=None):
        ns = int(FAULT_START.timestamp() * 1e9)
        return {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": service, "level": "error"},
             "values": [[str(ns), "ERROR redis timeout - pool exhausted"]]}]}}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        return {"traces_considered": 1, "batches": [{
            "resource": {"service.name": service},
            "spans": [{"traceID": "a" * 32, "spanID": "1" * 16, "operationName": "redis.command",
                       "startTime": int(FAULT_START.timestamp() * 1e6), "duration": 2_000_000}]}]}

    def query_metric_series(self, *, service, metric, start, end):
        raise RuntimeError("unused")


def request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id="run-ckpt-001", incident_id="inc-ckpt-001",
        environment="demo", service_hint="order-service",
        fault_start=FAULT_START, window_start=WINDOW_START, window_end=WINDOW_END,
    )


def initial_state() -> dict:
    """图初始状态。**不含 ctx**：运行时依赖经 config 传递（见 graph 的 CTX_CONFIG_KEY）。"""
    return {
        "request": request(), "plan": [], "evidence": [], "hypotheses": [], "links": [],
        "ranked": [], "evidence_graph_summary": {}, "report": None, "status": "RUNNING",
        "errors": [], "missing_data": [],
    }


def graph_config(thread_id: str, ctx: RunContext) -> dict:
    from traceops.orchestration.graph import CTX_CONFIG_KEY
    return {"configurable": {CTX_CONFIG_KEY: ctx, "thread_id": thread_id}}


def context(adapters=None) -> RunContext:
    return RunContext(store=EvidenceStore(),
                      adapters=adapters or CountingAdapters(),
                      budget=BudgetManager(max_tool_calls=20))


class TestCheckpointerSupport:
    def test_graph_accepts_checkpointer(self):
        graph = build_investigation_graph(checkpointer=MemorySaver())
        assert graph.checkpointer is not None

    def test_graph_without_checkpointer_still_works(self):
        assert build_investigation_graph().checkpointer is None

    def test_run_investigation_accepts_thread_id(self):
        ctx = context()
        result = run_investigation(request(), ctx, thread_id="thread-001")
        assert result.report is not None

    def test_same_thread_reaches_completed_state(self):
        """同一 thread 跑完后，checkpoint 里应留下终态。"""
        saver = MemorySaver()
        ctx = context()

        result = run_investigation(request(), ctx, thread_id="thread-002", checkpointer=saver)

        assert result.report is not None
        state = saver.get_tuple({"configurable": {"thread_id": "thread-002"}})
        assert state is not None
        assert state.checkpoint["channel_values"]["status"] in ("SUPPORTED", "INCONCLUSIVE")


class TestResume:
    def test_resume_continues_from_checkpoint_not_from_scratch(self):
        """核心断言：恢复时不会重跑已完成节点（否则会二次查询、二次消耗预算）。"""
        saver = MemorySaver()
        adapters = CountingAdapters()
        ctx = context(adapters)

        # 第一次：跑到 collect 之后人为中断（用 interrupt_before 模拟崩溃点）
        graph = build_investigation_graph(checkpointer=saver, interrupt_before=["hypothesize"])
        config = graph_config("thread-003", ctx)
        first = graph.invoke(initial_state(), config)
        assert first.get("evidence"), "collect must have run before the interruption"
        calls_after_first = adapters.error_rate_calls
        assert calls_after_first >= 1

        # 第二次：从 checkpoint 恢复，继续跑完
        resumed = graph.invoke(None, config)

        assert resumed.get("report") is not None, "resume must finish the investigation"
        # collect 不应被重跑：查询次数不应翻倍
        assert adapters.error_rate_calls == calls_after_first, (
            f"collect re-ran on resume: {adapters.error_rate_calls} vs {calls_after_first}")

    def test_interrupt_before_plan_leaves_nothing_collected(self):
        saver = MemorySaver()
        adapters = CountingAdapters()
        ctx = context(adapters)
        graph = build_investigation_graph(checkpointer=saver, interrupt_before=["plan"])
        graph.invoke(initial_state(), graph_config("thread-004", ctx))

        assert adapters.error_rate_calls == 0


class TestCheckpointDoesNotLeakState:
    def test_two_threads_do_not_share_checkpoint_state(self):
        saver = MemorySaver()
        ctx_a = context()
        ctx_b = context()

        result_a = run_investigation(request(), ctx_a, thread_id="thread-a", checkpointer=saver)
        result_b = run_investigation(request(), ctx_b, thread_id="thread-b", checkpointer=saver)

        assert result_a.report is not None
        assert result_b.report is not None
        # 两个 thread 的证据集合必须独立（id 不同）
        ids_a = set(result_a.evidence_ids)
        ids_b = set(result_b.evidence_ids)
        assert ids_a.isdisjoint(ids_b), "threads must not share evidence"
