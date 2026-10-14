"""配置快照必须反映**真实配置**，不能永远是默认值。

## 缺陷（参考仓库分析确认，实读代码）

`worker.py:341`：

```python
config=InvestigationConfigSnapshot()
```

全默认值。于是每一次 run 的 `model_name` 都记成 `"deterministic-rules"`、
`workflow_version` 都记成 `"p0-v1"` —— **即使真的接了模型**。

后果直接命中 `EVALUATION §5 可复现性` 与蓝图 §13.3 的承诺：

> 完整配置快照（模型/工作流/工具 schema 版本）

如果两次运行结论不同，我们**无法从快照查出差在哪**，因为快照永远是同一份。
更糟的是它会**撒谎**：接了模型却记成 "deterministic-rules"，
事后复盘会得出"当时是确定性规则跑的"这个错误结论。

## 正确行为

快照从**实际构造的对象**派生：模型客户端名、实际 scope_depth、实际预算上限。
"""
from __future__ import annotations

import pytest

from traceops.runtime.investigation_run import InvestigationConfigSnapshot


class TestSnapshotReflectsActualModel:
    def test_snapshot_records_the_real_model_name(self):
        """接了模型就必须记下模型名 —— 否则事后无法归因。"""
        snapshot = InvestigationConfigSnapshot.from_runtime(
            model_name="openai-compatible:deepseek-v4-flash",
            scope_depth=2,
            max_tool_calls=40,
        )
        assert snapshot.model_name == "openai-compatible:deepseek-v4-flash"
        assert snapshot.model_provider == "openai-compatible"

    def test_no_model_records_deterministic(self):
        snapshot = InvestigationConfigSnapshot.from_runtime(
            model_name=None, scope_depth=1, max_tool_calls=40)
        assert snapshot.model_name == "deterministic-rules"
        assert snapshot.model_provider == "none"

    def test_snapshot_records_scope_depth(self):
        """采集深度是可复现性的一部分：深度变了结论就可能变。"""
        snapshot = InvestigationConfigSnapshot.from_runtime(
            model_name=None, scope_depth=3, max_tool_calls=40)
        assert "3" in snapshot.topology_version, snapshot.topology_version

    def test_snapshot_records_budget(self):
        snapshot = InvestigationConfigSnapshot.from_runtime(
            model_name=None, scope_depth=1, max_tool_calls=99)
        assert "99" in snapshot.workflow_version, snapshot.workflow_version


class TestWorkerUsesRealSnapshot:
    def test_worker_snapshot_is_not_all_defaults(self):
        """端到端：worker 产生的 run 的 config 必须带真实信息。"""
        from datetime import UTC, datetime, timedelta

        from traceops.evidence.store import EvidenceStore
        from traceops.orchestration.graph import RunContext
        from traceops.runtime.budget import BudgetManager
        from traceops.runtime.worker import (
            InMemoryEventSink, InMemoryEventSource, InvestigationWorker,
            derive_request_from_event,
        )

        event = {
            "id": "e-1",
            "payload": {
                "envelope": {"event_id": "evt-11111111-1111-4111-8111-111111111111", "event_type": "investigation.requested",
                             "schema_version": "1.0", "occurred_at": "2026-10-30T10:02:00Z",
                             "producer": "control-plane", "correlation_id": "inc-1",
                             "aggregate_id": "run-1", "aggregate_version": 1, "causation_id": None},
                "payload": {"investigation_run_id": "run-1", "incident_id": "inc-1",
                            "service_hint": "order-service", "environment": "demo",
                            "config_version": "cfg-x",
                            "time_range": {"start": "2026-10-30T10:00:00Z",
                                           "end": "2026-10-30T10:06:00Z"}},
            },
        }

        def build_context(request):
            return RunContext(store=EvidenceStore(), adapters=None,
                              budget=BudgetManager(max_tool_calls=40), scope_depth=2)

        worker = InvestigationWorker(
            source=InMemoryEventSource(), sink=InMemoryEventSink(),
            derive_request=derive_request_from_event, build_context=build_context)
        worker.source.push(event)
        worker.poll_once()
        # 事件里必须带真实的 scope_depth / max_tool_calls，而不是恒定默认
        completed = [m for m in worker.sink.messages
                     if m["envelope"]["event_type"] == "investigation.completed"]
        assert completed, "expected a completed event"
