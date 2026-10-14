"""关键失败路径必须留下**日志记录**，而不只是把原因塞进结果对象。

## 缺陷（自查自己上一个提交时发现）

commit 33b4ce2 声称给核心运行时加上了结构化日志，标题是
"The core runtime had no logging at all"。它的测试验证了：

1. 三个核心模块**各自有一个 logger 对象**（`hasattr(module, "log")`）
2. worker 的一条特定 warning（"invalid investigation request"）被记录

但这两条都**没有验证关键失败路径真的会留下日志**。实际情况：

```
$ grep -rE "\blog\.(debug|info|warning|error|exception)\(" agent-runtime/traceops/
agent-runtime/traceops/runtime/worker.py:362:  log.warning("invalid investigation request",
```

**全仓只有 1 处日志调用。** graph.py 和 executor.py 定义了 logger
却一次都没调用 —— logger 是摆设。

后果与"完全没有日志"一样：数据源挂掉时 `graph.py` 的
`except Exception: missing.append(...); continue` 静默吞掉一切，
运维从外部只看到"少了几类证据"，看不到原因、没有 message_id、
没有异常类型。而"结构化日志"这个能力宣称与事实不符。

## 三条约束

1. **数据源失败要有日志**（不是只有 worker 的请求校验失败）
2. **日志要带结构化字段**（工具名 / 异常类型）——
   只写一句话等于没写，字段才是能被检索的
3. **executor 的执行失败也要有**（写操作的失败最需要痕迹）
"""
from __future__ import annotations

import logging

import pytest

from traceops.orchestration import graph as graph_module
from traceops.remediation import executor as executor_module


class TestCoreModulesActuallyLog:
    def test_graph_logs_a_datasource_failure(self, caplog):
        """数据源失败不能只进 missing_data 就消失。"""
        assert hasattr(graph_module, "log"), "graph must expose a logger"

        class _Boom:
            def invoke(self, *a, **k):
                raise RuntimeError("datasource exploded")

        from traceops.orchestration.graph import CTX_CONFIG_KEY, RunContext

        ctx = RunContext(store=_Store(), adapters=None, gateway=_Boom())
        state = {
            "request": _request(),
            "plan": [{"tool": "prometheus_query", "params": {"query": "up"},
                      "evidence_type": "metric",
                      "observed_at": "2026-10-30T10:02:00Z"}],
            "errors": [], "missing_data": [],
        }
        with caplog.at_level(logging.WARNING, logger="traceops.orchestration.graph"):
            graph_module._collect_node(
                state, {"configurable": {CTX_CONFIG_KEY: ctx}})

        records = [r for r in caplog.records
                   if getattr(r, "tool", None) == "prometheus_query"]
        assert records, (
            "a datasource failure left no log record; the reason only exists "
            "inside missing_data, which nobody sees")
        # 失败路径必须能被检索：异常类型与工具名要作为结构化字段。
        # 消息本身保持稳定短语（可被 grep/聚合），变量数据放字段里。
        rec = records[0]
        assert getattr(rec, "error_type", None) == "RuntimeError", (
            "the log record carries no exception type - unsearchable")
        assert "datasource exploded" in (getattr(rec, "reason", "") or ""), (
            "the log record does not carry the failure reason as a field")
        assert rec.exc_info, "the traceback is not attached - root cause is lost"

    def test_executor_logs_a_failed_execution(self, caplog):
        """写操作失败必须留痕 —— 这是最需要痕迹的一类失败。"""
        assert hasattr(executor_module, "log"), "executor must expose a logger"

        from traceops.remediation.executor import ExecutionStatus, execute_plan
        from traceops.remediation.policy import PolicyDecision

        class _Failing:
            def execute(self, action, target, parameters):
                raise RuntimeError("kubectl exploded")

        result = execute_plan(
            _plan(), policy=PolicyDecision(allowed=True, reasons=[], requires_approval=False),
            approval=None, registry=_registry(), executor=_Failing(),
            verifier=_Healthy(),
        )

        assert result.status == ExecutionStatus.FAILED
        records = [r for r in caplog.records
                   if getattr(r, "action", None) == "scale_pool"]
        assert records, "a failed remediation left no log record"
        rec = records[0]
        assert getattr(rec, "target", None) == "redis-connection-pool", (
            "the log record names no target - cannot tell what was changed")
        assert getattr(rec, "error_type", None) == "RuntimeError", (
            "the log record carries no exception type - unsearchable")
        assert "kubectl exploded" in (getattr(rec, "reason", "") or ""), (
            "the log record does not carry the failure reason as a field")
        assert rec.exc_info, "the traceback is not attached - root cause is lost"


class TestLoggerUsageIsNotDecorative:
    """防止再次出现"定义了 logger 却从不调用"的回归。"""

    @pytest.mark.parametrize("module_name", [
        "traceops.orchestration.graph",
        "traceops.remediation.executor",
        "traceops.runtime.worker",
    ])
    def test_module_calls_its_logger(self, module_name):
        """每个核心模块必须**真的调用** logger，而不只是定义它。

        原来的测试只检查 logger 对象存在 —— 一个从不调用的 logger
        完全能通过，这正是上一版测试放过缺陷的原因。
        """
        import importlib

        module = importlib.import_module(module_name)
        with open(module.__file__, encoding="utf-8") as handle:
            text = handle.read()
        calls = [
            line for line in text.splitlines()
            if "log." in line and "getLogger" not in line
            and not line.strip().startswith("#")
        ]
        assert calls, (
            f"{module_name} defines a logger but never calls it - "
            "failures in that module are invisible")


# --- 构造真实 API 所需的最小对象 ---

def _plan():
    from traceops.remediation.plan import RemediationPlan, RiskLevel

    return RemediationPlan(
        plan_id="plan-1", incident_id="inc-1", action="scale_pool",
        target="redis-connection-pool", parameters={"max_pool_size": 20},
        risk=RiskLevel.MEDIUM, requires_approval=True,
        rationale="restore maxPoolSize",
        rollback={"action": "scale_pool", "parameters": {"max_pool_size": 2}},
        post_conditions=["error_rate_below_threshold"],
    )


def _registry():
    from traceops.remediation.actions import ActionRegistry, ActionSpec
    from traceops.remediation.plan import RiskLevel

    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="scale_pool", description="resize pool", risk=RiskLevel.MEDIUM,
        requires_approval=True, allowed_targets={"redis-connection-pool"},
        parameter_schema={"type": "object",
                          "properties": {"max_pool_size": {"type": "integer", "minimum": 1}},
                          "required": ["max_pool_size"], "additionalProperties": False},
        post_conditions=["error_rate_below_threshold"],
    ))
    return registry


class _Healthy:
    def verify(self, plan):
        return True


class _Store:
    """证据存储的最小替身：本用例在收集阶段就失败了，不写盘。"""

    def put(self, *a, **k):
        raise AssertionError("evidence must not be stored in this scenario")


def _request():
    from datetime import UTC, datetime

    from traceops.orchestration.graph import InvestigationRequest

    return InvestigationRequest(
        investigation_run_id="run-1", incident_id="inc-1",
        service_hint="order-service", environment="demo",
        fault_start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
        window_start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
        window_end=datetime(2026, 10, 30, 10, 6, tzinfo=UTC),
    )


class TestWorkerFailurePathsLog:
    """worker 的三条失败路径也必须留痕：

    1. 调查整体失败（result.status == FAILED）
    2. 预算耗尽
    3. 未预期异常
    """

    def _worker(self, sink, ledger=None):
        from traceops.runtime.run_ledger import RunLedger
        from traceops.runtime.worker import (
            InMemoryEventSink, InMemoryEventSource, InvestigationWorker,
            derive_request_from_event,
        )

        from traceops.runtime.budget import BudgetManager
        from traceops.orchestration.graph import RunContext

        source = InMemoryEventSource()
        # build_context 必须给出真实的 RunContext —— worker 会从中取 budget。
        worker = InvestigationWorker(
            source=source, sink=sink,
            derive_request=derive_request_from_event,
            build_context=lambda r: RunContext(store=_Store(), adapters=None,
                                              budget=BudgetManager()),
            ledger=ledger or RunLedger())
        return source, worker

    def _message(self):
        return {
            "id": "msg-1",
            "payload": {
                "envelope": {
                    "event_id": "evt-11111111-1111-4111-8111-111111111111", "event_type": "investigation.requested",
                    "schema_version": "1.0", "occurred_at": "2026-10-30T10:02:00Z",
                    "producer": "control-plane", "correlation_id": "inc-1",
                    "aggregate_id": "run-1", "aggregate_version": 1,
                    "causation_id": None,
                },
                "payload": {
                    "investigation_run_id": "run-1", "incident_id": "inc-1",
                    "service_hint": "order-service", "environment": "demo",
                    "config_version": "cfg",
                    "time_range": {"start": "2026-10-30T10:00:00Z",
                                   "end": "2026-10-30T10:06:00Z"},
                },
            },
        }

    def test_invalid_request_is_logged_with_the_message_id(self, caplog):
        """非法请求：日志必须带 message_id，否则无法定位到具体那条消息。"""
        from traceops.runtime.worker import InMemoryEventSink

        source, worker = self._worker(InMemoryEventSink())
        source.push({"id": "msg-bad", "payload": {"nonsense": True}})
        with caplog.at_level(logging.WARNING, logger="traceops.runtime.worker"):
            worker.poll_once()
        records = [r for r in caplog.records
                   if getattr(r, "message_id", None) == "msg-bad"]
        assert records, "the rejected message left no log record"

    def test_unexpected_error_is_logged_with_traceback(self, caplog, monkeypatch):
        """未预期异常：必须有 traceback，否则根因丢失。"""
        from traceops.runtime import worker as worker_module
        from traceops.runtime.worker import InMemoryEventSink

        source, worker = self._worker(InMemoryEventSink())

        def _boom(*a, **k):
            raise RuntimeError("investigation exploded")

        monkeypatch.setattr(worker_module, "run_investigation", _boom)
        source.push(self._message())
        with caplog.at_level(logging.ERROR, logger="traceops.runtime.worker"):
            worker.poll_once()

        records = [r for r in caplog.records
                   if getattr(r, "run_id", None) == "run-1"]
        assert records, "an unexpected investigation error left no log record"
        assert records[0].exc_info, "the traceback is not attached"
        assert getattr(records[0], "error_type", None) == "RuntimeError"
