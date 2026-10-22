"""缓存省掉的是重复查询，不是时间。

## 被替换的缺陷

告警风暴会产生**同一服务、同一时间窗**的多条事故。每条都要跑一遍 12 条
查询，参数完全相同 —— 同样的数据被重复取回，而报告里写着"这是本次调查
的证据"，读者无从知道其中一份其实是十分钟前抓的。

## 两条同样重要

1. 命中缓存不扣预算：命中没有打数据源，算成一次取数成本会让"复用"看起来
   和"重查"一样贵，于是没人愿意开着它。
2. 命中必须留下**当初**的取数时刻：`observed_at` 说数据描述哪一刻，
   `collected_at` 说我们什么时候拿到它。用 `now()` 会让报告声称
   "这份日志是刚抓的"，复盘时"为什么当时没看到变化"会被这个时间戳骗过去。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import EvidenceType  # noqa: F401  (说明证据类型)
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetManager
from traceops.tools.gateway import ToolGateway, register_builtin_tools
from traceops.tools.query_cache import QueryCache

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = TS - timedelta(minutes=2)
WINDOW_END = TS + timedelta(minutes=3)
SAMPLE_AT = WINDOW_END - timedelta(seconds=30)


class _CountingAdapters:
    """记录每个方法被真正调用了几次 —— 缓存的全部价值都在这个计数上。"""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def query_service_error_rate(self, service, start, end):
        self._count("error_rate")
        return {"status": "success",
                "data": {"resultType": "matrix", "result": [
                    {"metric": {}, "values": [[SAMPLE_AT.timestamp(), "0.42"]]}]}}

    def query_service_logs(self, service, start, end, filter=None):
        self._count("logs")
        return {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        self._count("traces")
        return {"status": "success",
                "data": {"resultType": "traces", "result": []}}


def _request(suffix: str) -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id=f"run-{suffix}",
        incident_id=f"inc-{suffix}",
        environment="demo",
        service_hint="order-service",
        fault_start=TS,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )


def _ctx(adapters, cache, *, max_tool_calls: int = 50) -> RunContext:
    return RunContext(
        store=EvidenceStore(),
        adapters=adapters,
        budget=BudgetManager(max_tool_calls=max_tool_calls),
        cache=cache,
        scope_depth=0,
    )


# ---------- 1. Gateway：第二次同样的查询不再打数据源 ----------

class TestTheGatewayServesFromCache:
    def _gateway(self, adapters, cache):
        gateway = ToolGateway(cache=cache)
        register_builtin_tools(gateway, adapters)
        return gateway

    def test_an_identical_query_is_not_sent_twice(self):
        # 缓存是**跨调查**共享的：每条调查有自己的 Gateway（`_seen_calls`
        # 按调查隔离），共享的是 cache。同一条调查内重复相同查询仍然报错 ——
        # 那说明计划里有重复项，是要修的 bug，不是可以静默复用的情况。
        adapters = _CountingAdapters()
        cache = QueryCache()
        budget = BudgetManager(max_tool_calls=10)
        params = {"service": "order-service", "start": WINDOW_START.isoformat(),
                  "end": WINDOW_END.isoformat()}

        first = self._gateway(adapters, cache).invoke(
            "query_service_error_rate", params, budget=budget)
        second = self._gateway(adapters, cache).invoke(
            "query_service_error_rate", params, budget=budget)

        assert adapters.calls["error_rate"] == 1
        assert first == second

    def test_the_first_call_reports_no_hit_and_the_second_reports_one(self):
        adapters = _CountingAdapters()
        cache = QueryCache()
        budget = BudgetManager(max_tool_calls=10)
        params = {"service": "order-service", "start": WINDOW_START.isoformat(),
                  "end": WINDOW_END.isoformat()}

        first_gateway = self._gateway(adapters, cache)
        first_gateway.invoke("query_service_error_rate", params, budget=budget)
        first_note = first_gateway.take_cache_note()
        second_gateway = self._gateway(adapters, cache)
        second_gateway.invoke("query_service_error_rate", params, budget=budget)
        second_note = second_gateway.take_cache_note()

        assert first_note is None
        assert second_note is not None
        assert second_note["tool"] == "query_service_error_rate"

    def test_a_cache_hit_does_not_consume_the_tool_budget(self):
        # 命中没有打数据源。把它算成一次取数成本，等于惩罚"复用"
        adapters = _CountingAdapters()
        cache = QueryCache()
        budget = BudgetManager(max_tool_calls=10)
        params = {"service": "order-service", "start": WINDOW_START.isoformat(),
                  "end": WINDOW_END.isoformat()}

        self._gateway(adapters, cache).invoke(
            "query_service_error_rate", params, budget=budget)
        self._gateway(adapters, cache).invoke(
            "query_service_error_rate", params, budget=budget)

        assert budget.tool_calls == 1

    def test_the_note_is_taken_exactly_once(self):
        adapters = _CountingAdapters()
        cache = QueryCache()
        budget = BudgetManager(max_tool_calls=10)
        params = {"service": "order-service", "start": WINDOW_START.isoformat(),
                  "end": WINDOW_END.isoformat()}
        self._gateway(adapters, cache).invoke(
            "query_service_error_rate", params, budget=budget)
        gateway = self._gateway(adapters, cache)
        gateway.invoke("query_service_error_rate", params, budget=budget)

        assert gateway.take_cache_note() is not None
        # 留在里面会被下一次调用误认成自己的命中
        assert gateway.take_cache_note() is None

    def test_without_a_cache_an_identical_query_is_still_an_error(self):
        # 缓存在这里是**显式开启**的能力：默认行为不变（重复调用仍然报错）
        import pytest

        from traceops.tools.gateway import DuplicateToolCall

        adapters = _CountingAdapters()
        budget = BudgetManager(max_tool_calls=10)
        gateway = ToolGateway()
        register_builtin_tools(gateway, adapters)
        params = {"service": "order-service", "start": WINDOW_START.isoformat(),
                  "end": WINDOW_END.isoformat()}

        gateway.invoke("query_service_error_rate", params, budget=budget)
        with pytest.raises(DuplicateToolCall):
            gateway.invoke("query_service_error_rate", params, budget=budget)


# ---------- 2. 编排：第二条事故复用同一份数据，但时间戳不撒谎 ----------

class TestCachedEvidenceKeepsItsFetchTime:
    def test_the_second_investigation_does_not_touch_the_datasources(self):
        adapters = _CountingAdapters()
        cache = QueryCache()

        first = run_investigation(_request("first"), _ctx(adapters, cache))
        calls_after_first = dict(adapters.calls)
        second = run_investigation(_request("second"), _ctx(adapters, cache))

        assert second.status != "FAILED"
        # 同一窗口、同一服务、同一批查询：第二条事故一次外网都不该打
        assert adapters.calls == calls_after_first
        assert second.report.cache_hits
        assert {hit["service"] for hit in second.report.cache_hits} == {"order-service"}
        assert not first.report.cache_hits

    def test_cached_evidence_records_when_it_was_actually_fetched(self):
        adapters = _CountingAdapters()
        cache = QueryCache()

        first = run_investigation(_request("first"), _ctx(adapters, cache))
        second = run_investigation(_request("second"), _ctx(adapters, cache))

        first_metric = [ev for ev in first.report.evidence
                        if ev.query_template_id == "qt-error-rate-v1"][0]
        second_metric = [ev for ev in second.report.evidence
                         if ev.query_template_id == "qt-error-rate-v1"][0]

        # 同一份数据，取回它的时刻必须是**当初**那一刻 —— 不是第二条事故开跑的时刻
        assert second_metric.collected_at < datetime.now(UTC) - timedelta(milliseconds=1)
        # 与第一条调查记录的时刻一致（到微秒级只差"入缓存"与"归一化"两次取时钟）
        assert abs((second_metric.collected_at - first_metric.collected_at)
                   .total_seconds()) < 1.0
        # 数据描述的时刻不受缓存影响
        assert second_metric.observed_at == first_metric.observed_at

    def test_the_report_names_the_cache_hit_and_its_original_fetch_time(self):
        adapters = _CountingAdapters()
        cache = QueryCache()

        run_investigation(_request("first"), _ctx(adapters, cache))
        second = run_investigation(_request("second"), _ctx(adapters, cache))

        second_metric = [ev for ev in second.report.evidence
                         if ev.query_template_id == "qt-error-rate-v1"][0]
        hit = [h for h in second.report.cache_hits
               if h["tool"] == "query_service_error_rate"][0]

        # 报告里写的"当初取数时刻"必须就是证据自己记的那个时刻 ——
        # 两处不一致等于给复盘的人两个互相矛盾的答案
        assert hit["fetched_at"] == second_metric.collected_at.isoformat()
