"""降级必须自带替代读取路径。

## 缺陷

超过 `max_result_bytes` 的结果会被落盘，落盘失败时 Gateway 返回
`{_spill: {path: None, truncated: true, spill_failed: true, preview, error}}`。
编排层读不回全量，只能把这次查询记进 `missing_data` —— 从证据角度看是对的，
但对**读报告的人**来说信息是残缺的：

```
missing: query_service_error_rate: result was spilled but unreadable (...)
```

这句话回答了"为什么没有这条证据"，没有回答"那我现在怎么才能拿到它"。
事故复盘时最需要的恰恰是第二句：是稍后重试就能拿到（落盘偶发失败），
还是必须换查询条件（数据量本身太大）。

## 正确行为

每一条降级记录都必须带 `read` —— 一条**替代读取路径**：

- 落盘成功：`spill:<绝对路径>`，交给 `SpillStore.load/page` 读回；
- 落盘失败：`requery:<工具名> with a narrower window (...)`，并说明为什么
  同参数重查没用（同样的数据量会再次落盘失败）。

这条纪律来自 super-agent 的 `src/app/links.ts:30-31`：外链解析失败时不返回
空值，而是返回 `source_url_read: "feishu/<trigger>/message"` 这样的替代路径。
降级本身不丢人，丢人的是降级之后没人知道下一步该做什么。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest, RunContext, run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _big_metric_payload(series: int = 400) -> dict:
    """> 64KB 的 Prometheus matrix（采样点必须落在窗口内）。"""
    base = int(TS.timestamp())
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"service": "order-service",
                               "label": f"instance-{i:04d}-" + "x" * 60},
                    "values": [[base + j, "0.12"] for j in range(20)],
                }
                for i in range(series)
            ],
        },
    }


class _Adapters:
    def __init__(self) -> None:
        self.error_rate_calls = 0

    def query_service_error_rate(self, service, start, end):
        self.error_rate_calls += 1
        return _big_metric_payload()

    def query_service_logs(self, service, start, end, filter=None):
        return {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}


def _run(spill_root: Path):
    from traceops.tools.gateway import ToolGateway, register_builtin_tools

    topology = ServiceTopology.from_edges([("api-gateway", "order-service")])
    adapters = _Adapters()
    gateway = ToolGateway(spill_root=spill_root)
    register_builtin_tools(gateway, adapters)

    ctx = RunContext(
        store=EvidenceStore(), adapters=adapters,
        budget=BudgetManager(max_tool_calls=40),
        gateway=gateway, topology=topology, scope_depth=1,
    )
    request = InvestigationRequest(
        investigation_run_id="run-degraded", incident_id="inc-degraded",
        environment="demo", service_hint="order-service",
        fault_start=TS, window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
    )
    return run_investigation(request, ctx), adapters


class TestUnwritableSpillStillSaysHowToRead:
    def test_a_failed_spill_is_recorded_as_a_degraded_read(self, tmp_path):
        # 把 spill 根目录做成一个**普通文件**：mkdir 会抛 NotADirectoryError(OSError)。
        root_file = tmp_path / "spill-root-is-a-file"
        root_file.write_text("not a directory", encoding="utf-8")

        result, adapters = _run(root_file)
        report = result.report

        assert adapters.error_rate_calls >= 1, "查询应当真的发生过"
        assert report is not None
        assert report.degraded_reads, (
            "落盘失败的结果没有留下降级记录 —— 报告里看不出这次数据被截断了")

    def test_the_degraded_entry_carries_a_requery_path(self, tmp_path):
        root_file = tmp_path / "spill-root-is-a-file"
        root_file.write_text("not a directory", encoding="utf-8")

        result, _ = _run(root_file)
        entry = result.report.degraded_reads[0]

        assert entry["kind"] == "spill_unreadable"
        assert entry["tool"] == "query_service_error_rate"
        assert entry["bytes"] and entry["bytes"] > 65536
        read = entry["read"]
        assert read.startswith("requery:query_service_error_rate"), read
        # 只说"再查一次"没有用：同参数会得到同样大的结果。
        assert "narrower window" in read

    def test_the_missing_data_line_still_explains_the_cause(self, tmp_path):
        root_file = tmp_path / "spill-root-is-a-file"
        root_file.write_text("not a directory", encoding="utf-8")

        result, _ = _run(root_file)
        assert any("spilled but unreadable" in m for m in result.report.missing_data), (
            result.report.missing_data)

    def test_a_successful_spill_produces_no_degraded_read(self, tmp_path):
        """能读回全量时不该有降级记录 —— 否则降级列表会变成噪音。"""
        result, _ = _run(tmp_path)
        assert result.report.degraded_reads == []
