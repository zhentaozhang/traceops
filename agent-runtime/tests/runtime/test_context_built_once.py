"""`build_context` 每次调查只能被调用**一次**。

## 缺陷（worker.py）

```python
budget = BudgetManager(
    max_tool_calls=getattr(self.build_context(request).budget, "max_tool_calls", 50),
)                                    # ← 第 1 次：建 Store/Gateway/Budget 后丢弃
ctx = self.build_context(request)    # ← 第 2 次：真正使用的那个
```

第 1 次调用的产物被完全丢弃，只为读一个 `max_tool_calls` 数字。代价：

- **资源浪费**：真实实现里 `build_context` 会连数据源、开 SQLite 连接
- **状态不一致**：两次构造的 Budget 上限可能不同（如果工厂有副作用）
- **不可测试的假设**：调用方必须假定工厂是纯函数且幂等，
  但这条契约从未声明过

## 正确行为

一次构造，从**已构造的 ctx** 读上限。
"""
from __future__ import annotations

import pytest

from traceops.runtime.worker import InvestigationWorker


class CountingContext:
    """记录被构造的次数。"""

    def __init__(self):
        self.builds = 0

    def __call__(self, request):
        self.builds += 1
        from traceops.evidence.store import EvidenceStore
        from traceops.orchestration.graph import RunContext
        from traceops.runtime.budget import BudgetManager

        return RunContext(store=EvidenceStore(), adapters=None,
                          budget=BudgetManager(max_tool_calls=10))


def _worker(build):
    from traceops.runtime.worker import InMemoryEventSink, InMemoryEventSource, derive_request_from_event

    return InvestigationWorker(
        source=InMemoryEventSource(), sink=InMemoryEventSink(),
        derive_request=derive_request_from_event, build_context=build,
    )


class TestSingleBuild:
    def test_context_is_built_exactly_once(self):
        counter = CountingContext()
        worker = _worker(counter)
        # 用一条真实形状的事件驱动一次调查
        event = _valid_event()
        worker.source.push(event)
        worker.poll_once()

        assert counter.builds == 1, (
            f"build_context must be called once per investigation, got {counter.builds}")


def _valid_event() -> dict:
    return {
        "id": "e-1",
        "payload": {
            "envelope": {
                "event_id": "evt-1", "event_type": "investigation.requested",
                "schema_version": "1.0", "occurred_at": "2026-10-30T10:02:00Z",
                "producer": "control-plane", "correlation_id": "inc-1",
                "aggregate_id": "run-1", "aggregate_version": 1, "causation_id": None,
            },
            "payload": {
                "investigation_run_id": "run-1", "incident_id": "inc-1",
                "service_hint": "order-service", "environment": "demo",
                "config_version": "cfg",
                "time_range": {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:06:00Z"},
            },
        },
    }
