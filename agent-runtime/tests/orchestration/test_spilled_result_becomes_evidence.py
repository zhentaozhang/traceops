"""超大查询结果必须变成**证据**，而不是"缺失数据"。

## 缺陷

Gateway 对超过 `max_result_bytes`（默认 64KB）的结果返回指针
`{_spill: {path, digest, preview, truncated}}`，不是数据本身。
但 `_collect_node` 此前把这个指针直接交给 Normalizer：

```
normalize(raw_payload={'_spill': {...}})
→ ValueError: metric payload: expected Prometheus result shape {resultType, result}
→ except → missing_data.append(...) → continue
```

后果：**一次完全成功的查询，只因为数据量大，变成了"缺失数据"。**
真实规模下 Prometheus matrix / Loki streams 轻松超 64KB，
于是证据覆盖率和 citation precision 下降的根因居然是文件体积。

这条缺陷尤其隐蔽，因为：
- 没有任何异常冒到调用方（被 `except Exception` 吃掉）
- 审计日志说 `outcome=SPILLED`（"结果已保留"）
- 报告说"缺失数据"（没法查）

三处记录互相矛盾，而每一处单独看都"合理"。

## 正确行为

Spill 保护的是**上下文**，不是证据链。归一化在本进程内完成，
全量读回不占模型 token —— 所以编排层应当读全量、正常归一化，
并把落盘指针挂到 `Evidence.raw_snapshot_ref` 上（"全量在哪"可追溯）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest, RunContext, run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _big_metric_payload(series: int = 400) -> dict:
    """> 64KB 的 Prometheus matrix 响应（真实规模下很常见）。

    采样点必须落在请求的窗口内 —— 窗口外的观测会被 `Evidence` 的
    不变量拒绝，那样测的就不是 spill 了。
    """
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


class _BigResponseAdapters:
    """error_rate 返回一个超大 payload；其余查询返回空。"""

    def __init__(self, series: int = 400) -> None:
        self._series = series
        self.error_rate_calls = 0

    def query_service_error_rate(self, service, start, end):
        self.error_rate_calls += 1
        return _big_metric_payload(self._series)

    def query_service_logs(self, service, start, end, filter=None):
        return {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}


def _run(tmp_path: Path, series: int = 400):
    topology = ServiceTopology.from_edges([("api-gateway", "order-service")])

    from traceops.tools.gateway import ToolGateway, register_builtin_tools
    adapters = _BigResponseAdapters(series=series)
    gateway = ToolGateway(spill_root=tmp_path)
    register_builtin_tools(gateway, adapters)

    ctx = RunContext(
        store=EvidenceStore(), adapters=adapters,
        budget=BudgetManager(max_tool_calls=40),
        gateway=gateway, topology=topology, scope_depth=1,
    )
    request = InvestigationRequest(
        investigation_run_id="run-spill", incident_id="inc-spill",
        environment="demo", service_hint="order-service",
        fault_start=TS, window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
    )
    return run_investigation(request, ctx), adapters


def _payload_size(series: int) -> int:
    import json
    return len(json.dumps(_big_metric_payload(series), ensure_ascii=False).encode("utf-8"))


class TestOversizeResultBecomesEvidence:
    def test_the_payload_really_exceeds_the_gateway_limit(self):
        """前提校验：这份 payload 必须真的会被落盘，否则测的不是想要的东西。"""
        assert _payload_size(400) > 65536, _payload_size(400)

    def test_a_spilled_result_is_not_recorded_as_missing_data(self, tmp_path):
        result, adapters = _run(tmp_path)
        assert adapters.error_rate_calls >= 1, "查询应当真的发生过"
        metric_missing = [m for m in result.errors if "expected Prometheus result shape" in m]
        assert metric_missing == [], (
            f"spilled 结果被当成形状错误丢掉了：{metric_missing}")

    def test_a_spilled_result_produces_metric_evidence(self, tmp_path):
        result, _ = _run(tmp_path)
        report = result.report
        assert report is not None
        metric_evidence = [e for e in report.evidence if e.evidence_type.value == "metric"]
        assert metric_evidence, (
            "超大 payload 应当产生 metric 证据，实际证据类型："
            f"{[e.evidence_type.value for e in report.evidence]}")

    def test_evidence_records_where_the_full_payload_lives(self, tmp_path):
        """证据是归一化后的摘要，全量在 spill 文件里 —— 指针必须挂在证据上。"""
        result, _ = _run(tmp_path)
        report = result.report
        assert report is not None
        metric_evidence = [e for e in report.evidence if e.evidence_type.value == "metric"]
        assert metric_evidence
        ref = metric_evidence[0].raw_snapshot_ref
        assert ref, "落盘指针没有挂到证据上，全量原文不可追溯"
        assert Path(ref).exists(), f"指针指向的文件不存在：{ref}"

    def test_the_spilled_file_keeps_every_series(self, tmp_path):
        result, _ = _run(tmp_path)
        report = result.report
        assert report is not None
        metric_evidence = [e for e in report.evidence if e.evidence_type.value == "metric"]
        assert metric_evidence
        import json
        stored = json.loads(Path(metric_evidence[0].raw_snapshot_ref).read_text(encoding="utf-8"))
        assert len(stored["data"]["result"]) == 400, (
            "落盘必须是全量，不是预览")


class TestUnreadableSpillIsReportedNotSilent:
    def test_a_deleted_spill_file_becomes_visible_missing_data(self, tmp_path):
        """落盘文件被清理时，必须明确说"读不回"，而不是静默丢证据。"""
        from traceops.tools.gateway import SpillStore

        store = SpillStore(tmp_path)
        ref = store.save("query_service_error_rate", _big_metric_payload(400))
        Path(ref["path"]).unlink()

        gw_ref = {"_spill": ref}
        from traceops.tools.gateway import ToolGateway
        gw = ToolGateway(spill_root=tmp_path)
        with pytest.raises(FileNotFoundError):
            gw.load_spilled(gw_ref)
