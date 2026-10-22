"""拓扑漂移：声明的关系与实际被调用到的关系对不上时，报告必须说出来。

## 被替换的缺陷

`topology_edges` 是请求里带进来的**声明**，整趟调查把它当事实用
（`blast_radius`、`blame_order`、`_unobserved_related` 都基于它）。
声明会过期：新加的依赖还没人补声明、拆掉的服务还在文件里。
调查不会报错 —— 它会安静地按一张错的图推理。

最坏的一侧是"未声明"：拓扑里没有的依赖，`blast_radius` 覆盖不到，
它既不进影响范围、也不进 `_unobserved_related`（那份清单也是从拓扑推的）。
一个拓扑里根本不存在的依赖导致的全链路超时，在报告里连
"我没查过它"都不会出现。

## 为什么不需要 parentSpanId

OTLP 分支不提取 `parentSpanId`，无法从 span 还原调用父子关系。
但日志与链路文本会点名它碰了谁，`attribute_evidence` 已经把归属算完了：
沿它给出的归属，`(发起方, 被归属方)` 就是一条**确实被用到过**的边。

## 为什么反向那侧只叫"未取证"

声明了但本窗口没有任何证据的边，只说明"这次没被用到"，
不说明"断了"。把沉默当死亡是这套系统反复踩过的坑。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import Evidence, EvidenceType
from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.runtime.budget import BudgetManager
from traceops.evidence.store import EvidenceStore
from traceops.topology.discovery import diff_topology, discover_edges
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)
WINDOW_START = TS - timedelta(minutes=5)


def _log(service: str, message: str) -> Evidence:
    return Evidence(
        incident_id="inc-1",
        investigation_run_id="run-1",
        evidence_type=EvidenceType.LOG,
        source_id="loki",
        service_id=service,
        observed_at=TS,
        window_start=WINDOW_START,
        window_end=TS,
        query_template_id="qt-error-logs-v1",
        query_params={},
        normalized_data={
            "kind": "log",
            "records": [{"message": message, "level": "error"}],
            "total_records": 1,
            "truncated": False,
        },
        checksum="",
    )


def _trace(service: str, operation: str) -> Evidence:
    return Evidence(
        incident_id="inc-1",
        investigation_run_id="run-1",
        evidence_type=EvidenceType.TRACE,
        source_id="tempo",
        service_id=service,
        observed_at=TS,
        window_start=WINDOW_START,
        window_end=TS,
        query_template_id="qt-slow-traces-v1",
        query_params={},
        normalized_data={
            "kind": "trace",
            "spans": [{"operation_name": operation, "service": service,
                       "start_time": TS.timestamp(), "duration_us": 900_000}],
            "total_records": 1,
            "truncated": False,
        },
        checksum="",
    )


class TestEdgeDiscovery:
    def test_a_log_that_names_a_dependency_yields_an_edge(self):
        edges = discover_edges([
            _log("order-service",
                 "could not get a resource from the pool: redis connection refused"),
        ])

        assert ("order-service", "redis") in edges

    def test_a_span_that_names_a_dependency_yields_an_edge(self):
        edges = discover_edges([_trace("payment-service", "redis.command")])

        assert ("payment-service", "redis") in edges

    def test_metrics_are_not_edges(self):
        metric = Evidence(
            incident_id="inc-1", investigation_run_id="run-1",
            evidence_type=EvidenceType.METRIC, source_id="prometheus",
            service_id="order-service", observed_at=TS, window_start=WINDOW_START,
            window_end=TS, query_template_id="qt-error-rate-v1", query_params={},
            normalized_data={"kind": "metric", "result_type": "matrix",
                             "series": [{"metric": {}, "values": [[1.0, "0.4"]]}],
                             "result_count": 1, "total_records": 1},
            checksum="",
        )

        # 指标是服务自身状态，不是"谁调用了谁"
        assert discover_edges([metric]) == set()

    def test_a_log_without_a_dependency_yields_no_edge(self):
        assert discover_edges([_log("order-service", "health check ok")]) == set()


class TestDiff:
    def test_an_observed_edge_missing_from_the_mapping_is_drift(self):
        topology = ServiceTopology.from_edges([("checkout-service", "db")])

        drift = diff_topology(topology, {("order-service", "redis")})

        assert drift.undeclared_edges == [("order-service", "redis")]
        assert drift.has_drift is True

    def test_a_declared_edge_without_evidence_is_not_called_dead(self):
        topology = ServiceTopology.from_edges([("checkout-service", "db")])

        drift = diff_topology(topology, set())

        assert drift.unobserved_declared_edges == [("checkout-service", "db")]
        # 沉默不是断裂：`has_drift` 只由"未声明"这一侧触发
        assert drift.has_drift is False

    def test_without_a_topology_every_observed_edge_is_undeclared(self):
        drift = diff_topology(None, {("a", "b")})

        assert drift.declared_edges == []
        assert drift.undeclared_edges == [("a", "b")]

    def test_the_report_payload_lists_both_sides(self):
        topology = ServiceTopology.from_edges([("checkout-service", "db")])

        payload = diff_topology(topology, {("order-service", "redis")}).to_dict()

        assert payload["undeclared_edges"] == [["order-service", "redis"]]
        assert payload["unobserved_declared_edges"] == [["checkout-service", "db"]]
        assert payload["has_drift"] is True


class _LogsRevealRedis:
    """order-service 的日志点名 redis，但拓扑里只声明了 order-service→db。"""

    def query_service_error_rate(self, service, start, end):
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {}, "values": [[(TS - timedelta(seconds=30)).timestamp(), "0.42"]]}]}}

    def query_service_logs(self, service, start, end, filter=None):
        return {"status": "success", "data": {"resultType": "streams", "result": [{
            "values": [[str(int(TS.timestamp() * 1e9)),
                        "could not get a resource from the pool: redis connection refused"]]}]}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}


class TestTheReportCarriesTheDrift:
    def _run(self, topology):
        request = InvestigationRequest(
            investigation_run_id="run-drift",
            incident_id="inc-drift",
            environment="demo",
            service_hint="order-service",
            fault_start=TS,
            window_start=WINDOW_START,
            window_end=TS,
        )
        ctx = RunContext(
            store=EvidenceStore(),
            adapters=_LogsRevealRedis(),
            budget=BudgetManager(max_tool_calls=20),
            topology=topology,
            scope_depth=0,
        )
        return run_investigation(request, ctx)

    def test_an_undeclared_dependency_shows_up_in_the_report(self):
        result = self._run(ServiceTopology.from_edges([("order-service", "db")]))

        drift = result.report.topology_drift
        assert drift["undeclared_edges"] == [["order-service", "redis"]]

    def test_the_report_says_the_blast_radius_could_not_cover_it(self):
        result = self._run(ServiceTopology.from_edges([("order-service", "db")]))

        assert any("missing from the declared topology" in note
                   for note in result.report.missing_data)

    def test_no_topology_means_no_drift_section(self):
        result = self._run(None)

        # 没有声明就没有"漂移"可言：空字典区分于"对比过、没漂移"
        assert result.report.topology_drift == {}
