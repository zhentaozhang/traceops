"""Worker 测试：闭环连接点。

验证：
- 正常事件 → 调查 → completed 事件（带回可用的候选摘要）
- 重复投递 → 幂等跳过（不重复跑调查）
- 非法请求 → failed 事件，不重试
- 预算耗尽 → failed 事件，reason 是枚举
- 调查失败不留半成品报告
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext
from traceops.runtime.budget import BudgetManager
from traceops.runtime.investigation_run import RunStatus, InvestigationRun
from traceops.runtime.worker import (
    InMemoryEventSink,
    InMemoryEventSource,
    InvestigationWorker,
    derive_request_from_event,
    dumps,
)

FAULT_START = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 30, 10, 6, 0, tzinfo=UTC)


class OkAdapters:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def query_service_error_rate(self, *, service, start, end):
        self.calls.append(("error_rate", service))
        ts = int(FAULT_START.timestamp())
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": service}, "values": [[ts - 60, "0.01"], [ts, "0.4"]]}]}}

    def query_service_logs(self, *, service, start, end, filter=None):
        self.calls.append(("logs", service))
        ns = int(FAULT_START.timestamp() * 1e9)
        return {"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service": service, "level": "error"},
             "values": [[str(ns), "ERROR redis timeout - pool exhausted"]]}]}}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        self.calls.append(("traces", service))
        return {"traces_considered": 1, "batches": [{
            "resource": {"service.name": service},
            "spans": [{"traceID": "a" * 32, "spanID": "1" * 16, "operationName": "redis.command",
                       "startTime": int(FAULT_START.timestamp() * 1e6), "duration": 2_000_000}]}]}

    def query_metric_series(self, *, service, metric, start, end):
        raise RuntimeError("unused")


class NoDataAdapters(OkAdapters):
    def query_service_error_rate(self, *, service, start, end):
        raise RuntimeError("prometheus unreachable")

    def query_service_logs(self, *, service, start, end, filter=None):
        raise RuntimeError("loki unreachable")

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        raise RuntimeError("tempo unreachable")


def requested_event(run_id: str = "run-001", incident_id: str = "inc-001",
                    service_hint: str = "order-service") -> dict:
    return {
        "envelope": {
            "event_id": "evt-11111111-2222-3333-4444-555555555555",
            "event_type": "investigation.requested",
            "schema_version": "1.0",
            "occurred_at": WINDOW_END.isoformat(),
            "correlation_id": incident_id,
            "causation_id": None,
            "aggregate_id": run_id,
            "aggregate_version": 1,
            "producer": "control-plane",
        },
        "payload": {
            "investigation_run_id": run_id,
            "incident_id": incident_id,
            "environment": "demo",
            "service_hint": service_hint,
            "time_range": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
            "config_version": "cfg-test",
            "priority": "high",
        },
    }


def make_worker(adapters, *, source=None, sink=None, known=None, max_tool_calls=10):
    source = source or InMemoryEventSource()
    sink = sink or InMemoryEventSink()

    def build_context(request: InvestigationRequest) -> RunContext:
        return RunContext(store=EvidenceStore(), adapters=adapters,
                          budget=BudgetManager(max_tool_calls=max_tool_calls))

    return InvestigationWorker(source=source, sink=sink, derive_request=derive_request_from_event,
                               build_context=build_context, known_run_ids=known), source, sink


class TestDeriveRequest:
    def test_derives_request_from_contract_payload(self):
        request = derive_request_from_event(requested_event())
        assert request.investigation_run_id == "run-001"
        assert request.incident_id == "inc-001"
        assert request.service_hint == "order-service"
        assert request.fault_start == WINDOW_END
        assert request.window_start == WINDOW_START

    def test_rejects_wrong_producer(self):
        event = requested_event()
        event["envelope"]["producer"] = "agent-runtime"
        with pytest.raises(ValueError, match="producer"):
            derive_request_from_event(event)

    def test_rejects_wrong_event_type(self):
        event = requested_event()
        event["envelope"]["event_type"] = "investigation.completed"
        with pytest.raises(ValueError, match="event_type"):
            derive_request_from_event(event)

    def test_rejects_missing_service_hint(self):
        event = requested_event()
        del event["payload"]["service_hint"]
        with pytest.raises(ValueError, match="service_hint"):
            derive_request_from_event(event)


class TestWorkerHappyPath:
    def test_completes_run_and_publishes_completed_event(self):
        worker, source, sink = make_worker(OkAdapters())
        source.push(requested_event())

        stats = worker.poll_once()

        assert stats.succeeded == 1
        assert stats.failed == 0
        assert source.acked, "message must be acknowledged"
        events = [e["envelope"]["event_type"] for e in sink.messages]
        assert events == ["investigation.updated", "investigation.completed"]

        completed = sink.messages[-1]
        assert completed["envelope"]["producer"] == "agent-runtime"
        assert completed["envelope"]["aggregate_id"] == "run-001"
        assert completed["payload"]["rca_report_ref"] == "run-001/report"
        assert completed["payload"]["root_cause_candidates"], "must carry candidates"
        assert completed["payload"]["usage"]["tool_calls"] == 3
        # 大对象不进消息体
        assert "evidence" not in completed["payload"]

    def test_candidates_ranked_first(self):
        worker, source, sink = make_worker(OkAdapters())
        source.push(requested_event())

        worker.poll_once()

        candidates = sink.messages[-1]["payload"]["root_cause_candidates"]
        assert candidates[0]["rank"] == 1
        assert candidates[0]["candidate_service"] == "redis"

    def test_message_is_json_serialisable(self):
        worker, source, sink = make_worker(OkAdapters())
        source.push(requested_event())
        worker.poll_once()
        for message in sink.messages:
            assert json.loads(dumps(message)) == message


class TestWorkerIdempotency:
    def test_duplicate_run_id_is_skipped(self):
        worker, source, sink = make_worker(OkAdapters())
        source.push(requested_event())

        first = worker.poll_once()
        assert first.succeeded == 1

        source.push(requested_event())  # 同一 run_id 再来一次
        second = worker.poll_once()

        assert second.skipped_duplicate == 1
        assert second.processed == 0
        assert len(sink.messages) == 2, "duplicate must not produce new events"

    def test_known_run_ids_suppress_reprocessing(self):
        worker, source, sink = make_worker(OkAdapters(), known={"run-001"})
        source.push(requested_event())

        stats = worker.poll_once()

        assert stats.skipped_duplicate == 1
        assert sink.messages == []


class TestWorkerFailures:
    def test_invalid_request_emits_failed_event_without_retry(self):
        worker, source, sink = make_worker(OkAdapters())
        event = requested_event()
        del event["payload"]["service_hint"]
        source.push(event)

        stats = worker.poll_once()

        assert stats.failed == 1
        failed = sink.messages[-1]
        assert failed["payload"]["failure_reason"] == "INVALID_REQUEST"
        assert failed["payload"]["retryable"] is False

    def test_budget_exhaustion_is_not_retryable(self):
        worker, source, sink = make_worker(OkAdapters(), max_tool_calls=1)
        source.push(requested_event())

        stats = worker.poll_once()

        assert stats.failed == 1
        failed = sink.messages[-1]["payload"]
        assert failed["failure_reason"] == "BUDGET_EXHAUSTED"
        assert failed["retryable"] is False
        assert "completed" not in [e["envelope"]["event_type"] for e in sink.messages]

    def test_all_datasources_down_yields_inconclusive_not_failure(self):
        """数据源全挂是"证据不足"，不是"调查失败"—— 必须如实产出 INCONCLUSIVE。"""
        worker, source, sink = make_worker(NoDataAdapters())
        source.push(requested_event())

        stats = worker.poll_once()

        assert stats.succeeded == 1, "INCONCLUSIVE is a successful investigation"
        completed = sink.messages[-1]["payload"]
        assert completed["diagnosis_status"] == "INCONCLUSIVE"
        assert completed["abstained"] is True


class TestInvestigationRunStateMachine:
    def _run(self) -> InvestigationRun:
        return InvestigationRun.open(incident_id="inc-001")

    def test_full_happy_path(self):
        run = self._run()
        run.queue()
        run.start(node="plan")
        run.complete()
        assert run.status is RunStatus.COMPLETED
        assert run.status.terminal

    def test_approval_suspension_is_reversible(self):
        """挂起不是终点：必须能回到 RUNNING。"""
        run = self._run()
        run.queue()
        run.start(node="investigate")
        run.suspend_for_approval(node="remediate")
        assert run.status is RunStatus.WAITING_FOR_APPROVAL
        assert run.status.suspended
        assert not run.status.terminal

        run.resume(node="remediate")
        assert run.status is RunStatus.RUNNING
        run.complete()
        assert run.status is RunStatus.COMPLETED

    def test_cannot_resume_from_running(self):
        run = self._run()
        run.queue()
        run.start()
        with pytest.raises(Exception):
            run.resume()

    def test_terminal_states_have_no_outgoing_edges(self):
        for terminal in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            run = self._run()
            run.queue()
            if terminal is RunStatus.COMPLETED:
                run.start()
                run.complete()
            elif terminal is RunStatus.FAILED:
                run.start()
                run.fail(reason="boom")
            else:
                run.cancel()
            assert run.status is terminal
            with pytest.raises(Exception):
                run.cancel()

    def test_every_transition_bumps_aggregate_version(self):
        run = self._run()
        version = run.aggregate_version
        run.queue()
        assert run.aggregate_version == version + 1
        run.start()
        assert run.aggregate_version == version + 2

    def test_failure_keeps_checkpoint_for_recovery(self):
        run = self._run()
        run.queue()
        run.start(node="investigate")
        run.fail(reason="MCP down", node="investigate", checkpoint_id="ckpt-7")
        assert run.checkpoint_id == "ckpt-7"
        assert run.last_error == "MCP down"

    def test_usage_never_decreases(self):
        run = self._run()
        run.record_usage(tool_calls=5, tokens=1000)
        run.record_usage(tool_calls=3, tokens=500)  # 回填乱序
        assert run.tool_calls == 5
        assert run.tokens_used == 1000

    def test_snapshot_carries_config_for_reproducibility(self):
        run = self._run()
        snap = run.snapshot()
        assert snap["config"]["workflow_version"] == "p0-v1"
        assert snap["config"]["model_name"] == "deterministic-rules"
        assert snap["terminal"] is False


class TestMessageShapeTolerance:
    """两种合法输入形状都必须能直接工作。

    历史问题：Redis entry 里是一个 payload field 装着整份文档，而 worker 期望
    契约的两层形状。若不做归一，所有真实 Streams 消息都会被判为 INVALID_REQUEST，
    而调用方只能靠"记得包一层 adapter"才不炸 —— 这种隐藏依赖迟早出事。

    这里在 worker 入口归一，两种形状都测一遍。
    """

    def _flattened(self) -> dict:
        """Redis Streams entry 形状：外层 payload 装整份文档。"""
        doc = requested_event()
        return {"id": "1700000000000-1", "payload": doc}

    def test_flattened_shape_is_accepted(self):
        request = derive_request_from_event(self._flattened())
        assert request.investigation_run_id == "run-001"
        assert request.service_hint == "order-service"
        assert request.incident_id == "inc-001"

    def test_contract_shape_still_accepted(self):
        request = derive_request_from_event(requested_event())
        assert request.investigation_run_id == "run-001"

    def test_flattened_shape_end_to_end_through_worker(self):
        worker, source, sink = make_worker(OkAdapters())
        source.push(self._flattened())

        stats = worker.poll_once()

        assert stats.succeeded == 1
        events = [m["envelope"]["event_type"] for m in sink.messages]
        assert events == ["investigation.updated", "investigation.completed"]

    def test_flattened_shape_still_rejects_wrong_producer(self):
        doc = requested_event()
        doc["envelope"]["producer"] = "agent-runtime"
        with pytest.raises(ValueError, match="producer"):
            derive_request_from_event({"payload": doc})

    def test_malformed_shape_does_not_create_request(self):
        """缺字段时明确报错，而不是构造一个半成品请求。"""
        with pytest.raises(ValueError, match="service_hint"):
            derive_request_from_event({"payload": {"payload": {
                "investigation_run_id": "run-x", "incident_id": "inc-x",
                "environment": "demo",
                "time_range": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
                "config_version": "cfg",
            }}})


class TestPersistentStoreIntegration:
    """worker 跑完后证据必须落盘 —— 否则进程重启后无法审计。"""

    def _worker_with_db(self, db_path):
        from traceops.evidence.store import EvidenceStore
        from traceops.runtime.worker import (
            InMemoryEventSink, InMemoryEventSource, InvestigationWorker,
            derive_request_from_event,
        )

        store = EvidenceStore(path=db_path)
        source = InMemoryEventSource()
        sink = InMemoryEventSink()
        worker = InvestigationWorker(
            source=source, sink=sink, derive_request=derive_request_from_event,
            build_context=lambda r: RunContext(store=store, adapters=OkAdapters(),
                                               budget=BudgetManager(max_tool_calls=10)))
        return worker, source, sink, store

    def test_evidence_persisted_after_run(self, tmp_path):
        db = tmp_path / "evidence.db"
        worker, source, sink, store = self._worker_with_db(db)
        source.push(requested_event())
        worker.poll_once()

        # 用全新 Store 从同一个文件读，证据必须还在
        from traceops.evidence.store import EvidenceStore
        reopened = EvidenceStore(path=db)
        rows = reopened.list(incident_id="inc-001")
        assert rows, "evidence must survive process restart"
        assert all(reopened.validate(r.id, incident_id="inc-001").checks["checksum"] == "PASS"
                   for r in rows)
