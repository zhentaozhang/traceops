"""调查进度必须真的被上报，否则 SSE 只能显示"正在跑"。

## 缺陷

`contracts/events/v1/investigation-updated.schema.json` 声明了：

```
progress: {step_index, step_description, evidence_count, ...}
current_node
```

但两端**从未填充 progress**，而且整个调查期间只发**一条** updated 事件
（`started_event`，status=RUNNING, current_node=plan）。

对 SRE 来说这意味着：一个跑 30 秒的调查，前端只有"RUNNING"这一个状态，
看不到走到了哪一步、已经拿到多少证据。P2 的"调查进度实时推送"因此
**无法实现** —— 不是缺 SSE 传输，是缺数据。

## 正确行为

调查图执行到每个节点时产出一条进度事件，带：

- `current_node`：节点名（plan/collect/hypothesize/...）
- `progress.step_index`：单调递增，从 0 开始
- `progress.step_description`：人可读
- `progress.evidence_count`：当前已收集证据数（collect 后才有意义）

**单调递增是硬要求**：SSE 客户端可能乱序收到消息，靠 step_index 才能重排。
用 `aggregate_version` 重排不行 —— 它同时被终态事件消耗。
"""
from __future__ import annotations

import pytest

from traceops.runtime.investigation_run import InvestigationRun


class TestProgressPayload:
    def test_progress_event_carries_step_index(self):
        run = _running_run()
        event = _progress_event(run, node="collect", step_index=1,
                                description="collecting evidence", evidence_count=3)
        payload = event["payload"]
        assert payload["current_node"] == "collect"
        assert payload["progress"]["step_index"] == 1
        assert payload["progress"]["evidence_count"] == 3

    def test_step_index_monotonic(self):
        run = _running_run()
        events = [
            _progress_event(run, node="plan", step_index=0, description="planning"),
            _progress_event(run, node="collect", step_index=1, description="collecting"),
            _progress_event(run, node="hypothesize", step_index=2, description="hypothesizing"),
        ]
        indices = [e["payload"]["progress"]["step_index"] for e in events]
        assert indices == [0, 1, 2], "step_index must be monotonic for SSE reordering"

    def test_progress_matches_contract(self):
        """产出的 payload 必须过契约校验 —— 否则 Java 侧会拒绝。"""
        import json
        from pathlib import Path
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012

        contracts = Path(__file__).resolve().parents[3] / "contracts"
        schema = json.loads((contracts / "events/v1/investigation-updated.schema.json").read_text())
        common = json.loads((contracts / "common/event-envelope.schema.json").read_text())
        registry = Registry().with_resource(
            common["$id"], Resource.from_contents(common, default_specification=DRAFT202012))
        validator = Draft202012Validator(schema, registry=registry)

        run = _running_run()
        event = _progress_event(run, node="collect", step_index=1,
                                description="collecting evidence", evidence_count=2)
        errors = list(validator.iter_errors(event))
        assert not errors, errors


def _running_run():
    """构造一个**已启动**的 run。

    契约要求 envelope.aggregate_version >= 1，而 `open()` 之后未转换时是 0。
    真实路径里 run 一定先 queue -> start 才有事件，所以测试也必须这样构造 --
    直接用 open() 造事件会触发一个线上不会出现的契约违规。
    """
    run = InvestigationRun.open(incident_id="inc-1", run_id="run-1")
    run.queue()
    run.start(node="plan")
    return run


def _progress_event(run, *, node, step_index, description, evidence_count=0):
    from traceops.runtime.worker import progress_event
    return progress_event(run, node=node, step_index=step_index,
                          description=description, evidence_count=evidence_count)
