"""空结果**不是证据** —— 「查了，没有」必须和「查到了」分开。

## 为什么这是地基

一次查询返回空，和一次查询返回内容，是两件完全不同的事：

- 返回空 = 我们查过了，这个时间窗里**没有**这类观测
- 返回内容 = 我们**看见了**这些观测

把它们混为一谈会让报告拿"没看见"当"看见了"，这正是 aurora-lab
反复记录的失败模式：`absence of evidence into fabricated findings`。

## 这个 bug 是怎么出现的

评测 harness 之前用**抛异常**表达"这个场景没声明这类信号"：

```python
raise ScenarioDataUnavailable(f"{scenario_id}: no slow spans declared for {service}")
```

异常冒到 `_collect_node` 的 `except Exception`，走 missing_data 分支 ——
于是"没声明"被记成"查不了"。语义反了。

改对之后（返回空 payload），语义对了，但暴露出**下一个**缺陷：
`MetricNormalizer` / `LogNormalizer` 接受空输入并产出
`series: []` / `records: []` 的**证据对象**。于是：

- 报告引用了一批**零记录**的证据（`citation_precision` 被稀释）
- 这些空证据的 `service_id` 让服务进了"已观测"集合
  （`_observed_services` 已经在用 records 数量把关，但 `chain.py`
  的 `evidence_by_service` 只看 service_id，不看有没有记录）
- 传播链因此多出"我们其实什么都没看见"的节点

`TraceNormalizer` 早就**拒绝**空输入（`test_rejects_empty_trace`），
metric/log 却没有 —— **三种归一化器的空输入策略不一致**，
这才是根因。

## 正确行为

空 payload → **不产生证据**，但**必须留下痕迹**：

- 不进 `evidence`（不能拿空东西当证据引用）
- 不进 `missing_data` 的"失败"清单（它不是失败，是"查了，没有"）
- 但要在**别处**说清楚"查过但没有" —— 否则"没查"和"查了没有"又混了

本测试先钉住前两条；第三条（可观测的 no-data 计数）由
`no_data` 字段承载，见 `InvestigationResult.no_data_queries`。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest, RunContext, run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


class _EmptyAdapters:
    """每个查询都成功返回**空**结果 —— 「查了，没有」。"""

    def query_service_error_rate(self, service, start, end):
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    def query_service_logs(self, service, start, end, filter=None):
        return {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}


def _run():
    topology = ServiceTopology.from_edges([
        ("api-gateway", "order-service"),
        ("order-service", "redis"),
    ])
    ctx = RunContext(
        store=EvidenceStore(), adapters=_EmptyAdapters(),
        budget=BudgetManager(max_tool_calls=40),
        topology=topology, scope_depth=2,
    )
    request = InvestigationRequest(
        investigation_run_id="run-empty", incident_id="inc-empty",
        environment="demo", service_hint="order-service",
        fault_start=TS, window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
    )
    return run_investigation(request, ctx)


class TestEmptyResultIsNotEvidence:
    def test_an_empty_query_produces_no_evidence(self):
        """查了但什么都没有 → 报告里一条证据都不该有。"""
        result = _run()
        report = result.report
        assert report is not None
        assert report.evidence == [], (
            f"空结果不该变成证据，实际 {len(report.evidence)} 条")

    def test_an_empty_query_does_not_become_a_cited_evidence_link(self):
        """没有证据 → 不该有"证据支持假设"的关联。"""
        result = _run()
        report = result.report
        assert report is not None
        assert report.hypothesis_evidence == [], (
            f"空结果不该被当成支持证据，实际 {report.hypothesis_evidence}")

    def test_an_empty_query_is_not_reported_as_a_failure(self):
        """空结果不是"查询失败" —— 不能进 missing_data 的失败清单。

        它和失败的区别正是 NO_DATA vs ERROR：一个是"查了，没有"，
        一个是"没法查"。把前者记成后者，会让报告永远 abstain，
        而且让运维以为数据源坏了。
        """
        result = _run()
        report = result.report
        assert report is not None
        offenders = [
            item for item in report.missing_data
            if "no slow spans declared" in item or "no error logs declared" in item
            or "no error-rate series declared" in item
        ]
        assert not offenders, f"空结果不该被记成失败：{offenders}"

    def test_the_no_data_queries_are_still_visible(self):
        """但"查过、没数据"**必须留下痕迹** —— 否则又和"没查"混了。

        痕迹的载体是 `no_data_queries`（结构化），不是 missing_data
        （那是失败清单）。
        """
        result = _run()
        assert result.no_data_queries, (
            "查过但没数据的查询必须被记录下来，否则和'没查'无法区分")


class TestNormalizersAgreeOnEmptyInput:
    """三种归一化器对空输入必须是**同一个**策略。

    之前只有 TraceNormalizer 拒绝空输入，metric/log 接受 ——
    于是"空 trace"被丢弃，"空 metric"变成一条零记录的证据。
    同一件事，两种结果。
    """

    def test_metric_normalizer_rejects_an_empty_result(self):
        from traceops.evidence.normalizer import normalize
        from traceops.domain.models import EvidenceType
        import pytest

        with pytest.raises(ValueError, match="metric"):
            normalize(evidence_type=EvidenceType.METRIC, source_id="prometheus",
                      incident_id="inc-1", run_id="run-1", service_id="redis",
                      query_template_id="q", query_params={},
                      raw_payload={"resultType": "matrix", "result": []},
                      observed_at=TS, window=(TS - timedelta(minutes=2), TS))

    def test_log_normalizer_rejects_an_empty_result(self):
        from traceops.evidence.normalizer import normalize
        from traceops.domain.models import EvidenceType
        import pytest

        with pytest.raises(ValueError, match="log"):
            normalize(evidence_type=EvidenceType.LOG, source_id="loki",
                      incident_id="inc-1", run_id="run-1", service_id="redis",
                      query_template_id="q", query_params={},
                      raw_payload={"streams": []},
                      observed_at=TS, window=(TS - timedelta(minutes=2), TS))
