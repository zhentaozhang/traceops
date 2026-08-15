"""证据关联测试（蓝图 §6.3 / §6.4）。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import Evidence, EvidenceType
from traceops.evidence.graph import (
    CAUSAL_PRECEDES,
    SAME_TRACE,
    SERVICE_LINK,
    TEMPORAL,
    build_evidence_graph,
    earliest_anomaly,
    ordering_violations,
)
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW = (TS - timedelta(minutes=10), TS + timedelta(minutes=10))


def ev(service: str, etype: EvidenceType, *, at: datetime | None = None,
       trace_id: str | None = None, template: str = "qt-v1") -> Evidence:
    return Evidence(
        incident_id="inc-001", investigation_run_id="run-001",
        evidence_type=etype, source_id="src",
        service_id=service, observed_at=at or TS,
        window_start=WINDOW[0], window_end=WINDOW[1],
        query_template_id=template, query_params={},
        normalized_data={"kind": etype.value}, trace_id=trace_id,
    )


def topology() -> ServiceTopology:
    return ServiceTopology.from_edges([("order-service", "redis")])


class TestGraphConstruction:
    def test_all_evidence_nodes_added(self):
        items = [ev("redis", EvidenceType.METRIC), ev("order-service", EvidenceType.LOG)]
        graph = build_evidence_graph(items)
        assert set(graph.evidence) == {e.id for e in items}

    def test_services_collected(self):
        graph = build_evidence_graph([ev("redis", EvidenceType.METRIC), ev("order-service", EvidenceType.LOG)])
        assert graph.services() == {"redis", "order-service"}

    def test_filter_by_type(self):
        metric = ev("redis", EvidenceType.METRIC)
        log = ev("order-service", EvidenceType.LOG)
        graph = build_evidence_graph([metric, log])
        assert [e.id for e in graph.of_type(EvidenceType.METRIC)] == [metric.id]

    def test_timeline_is_chronological(self):
        early = ev("redis", EvidenceType.LOG, at=TS - timedelta(seconds=30))
        late = ev("order-service", EvidenceType.LOG, at=TS)
        graph = build_evidence_graph([late, early])  # 故意乱序传入
        times = [t for t, _ in graph.timeline()]
        assert times == sorted(times)
        assert graph.timeline()[0][1] == early.id


class TestCorrelationRules:
    def test_same_trace_link_preferred_over_temporal(self):
        a = ev("order-service", EvidenceType.LOG, trace_id="t-1")
        b = ev("order-service", EvidenceType.TRACE, trace_id="t-1")
        graph = build_evidence_graph([a, b])
        rules = {e.rule for e in graph.edges}
        assert SAME_TRACE.name in rules
        # 同一 trace 时不再额外加纯时间关联，避免重复边
        assert TEMPORAL.name not in rules

    def test_temporal_link_for_unrelated_evidence(self):
        a = ev("redis", EvidenceType.METRIC, at=TS)
        b = ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=20))
        graph = build_evidence_graph([a, b])
        assert any(e.rule == TEMPORAL.name for e in graph.edges)
        temporal = next(e for e in graph.edges if e.rule == TEMPORAL.name)
        assert temporal.time_delta_seconds == 20.0

    def test_no_link_outside_correlation_window(self):
        # 关联窗口（默认 5 分钟）小于证据自身的合法时间窗（±10 分钟），
        # 所以这里能构造"合法证据但超出关联窗口"的用例
        a = ev("redis", EvidenceType.METRIC, at=TS)
        b = ev("order-service", EvidenceType.LOG, at=TS + timedelta(minutes=8))
        graph = build_evidence_graph([a, b])
        assert graph.edges == []

    def test_service_link_only_when_topology_related(self):
        a = ev("order-service", EvidenceType.LOG, at=TS)
        b = ev("redis", EvidenceType.TRACE, at=TS + timedelta(seconds=5))
        graph = build_evidence_graph([a, b], topology=topology())
        assert any(e.rule == SERVICE_LINK.name for e in graph.edges)

    def test_no_service_link_without_topology(self):
        a = ev("order-service", EvidenceType.LOG, at=TS)
        b = ev("redis", EvidenceType.TRACE, at=TS + timedelta(seconds=5))
        graph = build_evidence_graph([a, b], topology=None)
        assert not any(e.rule == SERVICE_LINK.name for e in graph.edges)

    def test_unrelated_services_not_linked(self):
        a = ev("order-service", EvidenceType.LOG, at=TS)
        b = ev("billing-api", EvidenceType.TRACE, at=TS + timedelta(seconds=5))
        graph = build_evidence_graph([a, b], topology=topology())
        assert not any(e.rule == SERVICE_LINK.name for e in graph.edges)

    def test_change_event_anchors_timeline(self):
        change = ev(None, EvidenceType.EVENT, at=TS - timedelta(minutes=2))  # type: ignore[arg-type]
        symptom = ev("order-service", EvidenceType.LOG, at=TS)
        graph = build_evidence_graph([change, symptom])
        assert any(e.rule == CAUSAL_PRECEDES.name for e in graph.edges)


class TestDeterminism:
    def test_same_input_same_edges(self):
        items = [ev("redis", EvidenceType.METRIC), ev("order-service", EvidenceType.LOG)]
        g1 = build_evidence_graph(items)
        g2 = build_evidence_graph(list(reversed(items)))
        assert [(e.source_id, e.target_id, e.rule) for e in g1.edges] == \
               [(e.source_id, e.target_id, e.rule) for e in g2.edges]


class TestRootCauseTiming:
    def test_earliest_anomaly_ignores_change_events(self):
        change = ev(None, EvidenceType.EVENT, at=TS - timedelta(minutes=3))  # type: ignore[arg-type]
        metric = ev("redis", EvidenceType.METRIC, at=TS)
        found = earliest_anomaly([change, metric])
        assert found is not None
        assert found.id == metric.id

    def test_earliest_anomaly_returns_none_when_only_events(self):
        assert earliest_anomaly([ev(None, EvidenceType.EVENT, at=TS)]) is None  # type: ignore[arg-type]

    def test_ordering_violations_detects_symptom_before_root(self):
        """症状先于根因出现 = 时间线被污染（时钟漂移或 ground truth 标错）。"""
        root = ev("redis", EvidenceType.LOG, at=TS)
        symptom = ev("order-service", EvidenceType.LOG, at=TS - timedelta(seconds=30))
        violations = ordering_violations([root, symptom], expected_order=["redis", "order-service"])
        assert violations
        assert "order-service" in violations[0]

    def test_ordering_ok_when_root_first(self):
        root = ev("redis", EvidenceType.LOG, at=TS)
        symptom = ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=30))
        assert ordering_violations([root, symptom], expected_order=["redis", "order-service"]) == []

    def test_ordering_skips_missing_services(self):
        root = ev("redis", EvidenceType.LOG, at=TS)
        assert ordering_violations([root], expected_order=["redis", "api-gateway"]) == []


class TestRelationPromotion:
    """证据图必须能产出 EvidenceRelation，而不只是内部的 EvidenceEdge。

    P0 的 `EvidenceEdge` 是内部表示（rule + explanation）；
    R2 的 `EvidenceRelation` 是对外领域对象（可持久化、可查询、可审计）。
    两者必须能转换，否则关系只能活在内存里，重启即丢。
    """

    def test_graph_edges_promoted_to_domain_relations(self):
        from traceops.domain.models import EvidenceRelationType
        from traceops.evidence.graph import promote_relations

        a = ev("redis", EvidenceType.LOG, at=TS, trace_id="t-1")
        b = ev("order-service", EvidenceType.TRACE, at=TS + timedelta(seconds=10), trace_id="t-1")
        graph = build_evidence_graph([a, b])

        relations = promote_relations(graph)

        assert relations
        assert all(r.source_evidence_id and r.target_evidence_id for r in relations)
        assert any(r.relation_type is EvidenceRelationType.SAME_TRACE for r in relations)

    def test_temporal_edge_promoted_to_precedes(self):
        from traceops.domain.models import EvidenceRelationType
        from traceops.evidence.graph import promote_relations

        a = ev("redis", EvidenceType.METRIC, at=TS)
        b = ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=20))
        graph = build_evidence_graph([a, b])

        relations = promote_relations(graph)
        precedes = [r for r in relations if r.relation_type is EvidenceRelationType.PRECEDES]
        assert precedes
        assert precedes[0].time_delta_seconds == 20.0

    def test_service_link_promoted_to_affects(self):
        from traceops.domain.models import EvidenceRelationType
        from traceops.evidence.graph import promote_relations

        a = ev("order-service", EvidenceType.LOG, at=TS)
        b = ev("redis", EvidenceType.TRACE, at=TS + timedelta(seconds=5))
        graph = build_evidence_graph([a, b], topology=topology())

        relations = promote_relations(graph)
        affects = [r for r in relations if r.relation_type is EvidenceRelationType.AFFECTS]
        assert affects, [r.relation_type for r in relations]

    def test_promotion_never_creates_self_relations(self):
        """模型层拒绝自环 —— 提升逻辑必须保证不构造出非法关系。"""
        from traceops.evidence.graph import promote_relations

        graph = build_evidence_graph([ev("redis", EvidenceType.METRIC)])
        relations = promote_relations(graph)
        assert all(r.source_evidence_id != r.target_evidence_id for r in relations)

    def test_promotion_is_deterministic(self):
        from traceops.evidence.graph import promote_relations

        items = [ev("redis", EvidenceType.LOG, at=TS), ev("order-service", EvidenceType.LOG, at=TS)]
        g1 = build_evidence_graph(items)
        g2 = build_evidence_graph(list(reversed(items)))
        r1 = [(r.source_evidence_id, r.target_evidence_id, r.relation_type) for r in promote_relations(g1)]
        r2 = [(r.source_evidence_id, r.target_evidence_id, r.relation_type) for r in promote_relations(g2)]
        assert r1 == r2
