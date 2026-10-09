"""传播链（PropagationChain）测试。

这是修复 `chain=0.00` 的正解：把「候选根因」和「传播链节点」拆成两个概念。

之前的错误：传播链校验要求链上每一跳都出现在 hypotheses 里。
但 api-gateway 的证据被正确归因给了 redis，它自己**不该**有候选假设
（它没有独立故障机制）。硬要它当候选，等于让系统编造一个假故障。

正确模型：
- hypotheses   = "哪些服务/组件可能坏了"（候选根因）
- propagation_chain = "故障沿着什么路径传播"（含根因与受影响节点）

链上每一跳**必须有证据支撑**。没有证据的节点不许进链——否则就是拿拓扑编故事。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import (
    Evidence,
    EvidenceType,
    HypothesisStatus,
    RootCauseHypothesis,
)
from traceops.rca.chain import PropagationNode, build_propagation_chain
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW = (TS - timedelta(minutes=10), TS + timedelta(minutes=10))


def ev(service: str, etype: EvidenceType, *, at: datetime | None = None) -> Evidence:
    """构造一条**真实形状**的证据。

    `normalized_data` 必须带记录：归一化器对空结果一律拒绝
    （metric / log / trace 三者一致），所以"零记录证据"在生产里已经不存在。
    这里写出的形状要和归一化器的输出一致，否则测试就在验证一个
    现实中不存在的状态。
    """
    payload: dict = {"kind": etype.value, "total_records": 1, "result_count": 1}
    if etype is EvidenceType.LOG:
        payload["records"] = [{"timestamp": TS.isoformat(), "message": "ERROR pool exhausted",
                               "labels": {"level": "ERROR"}}]
    elif etype is EvidenceType.METRIC:
        payload["series"] = [{"metric": {"service": service}, "values": [[TS.timestamp(), "0.4"]]}]
    elif etype is EvidenceType.TRACE:
        payload["spans"] = [{"operation_name": "redis GET", "duration_us": 3_000_000}]
    return Evidence(
        incident_id="inc-001", investigation_run_id="run-001",
        evidence_type=etype, source_id="src", service_id=service,
        observed_at=at or TS, window_start=WINDOW[0], window_end=WINDOW[1],
        query_template_id="qt-v1", query_params={},
        normalized_data=payload,
    )


def zero_record_ev(service: str) -> Evidence:
    """一条**零记录**的证据 —— 「查了，什么都没有」。

    生产里归一化器会拒绝这种输入，但链构建必须自己守住这道闸：
    链上每一跳都声称"我们观测到了这个服务"。
    """
    return Evidence(
        incident_id="inc-001", investigation_run_id="run-001",
        evidence_type=EvidenceType.LOG, source_id="src", service_id=service,
        observed_at=TS, window_start=WINDOW[0], window_end=WINDOW[1],
        query_template_id="qt-v1", query_params={},
        normalized_data={"kind": "log", "total_records": 0, "records": []},
    )


def hypothesis(service: str, *, rank: int, fault_type: str = "pool_exhaustion") -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="run-001", candidate_service=service,
        fault_type=fault_type, failure_mechanism="m",
        predicted_observations=[], status=HypothesisStatus.SUPPORTED, rank=rank,
    )


def topology() -> ServiceTopology:
    # api-gateway → order-service → redis
    return ServiceTopology.from_edges([
        ("api-gateway", "order-service"),
        ("order-service", "redis"),
    ])


class TestChainConstruction:
    def test_root_plus_evidenced_dependents(self):
        """redis 是根因；order-service / api-gateway 有证据，构成链上的受影响节点。"""
        evidence_list = [
            ev("redis", EvidenceType.LOG, at=TS),
            ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=20)),
            ev("api-gateway", EvidenceType.TRACE, at=TS + timedelta(seconds=40)),
        ]
        chain = build_propagation_chain(
            root_service="redis",
            evidence_list=evidence_list,
            topology=topology(),
        )

        assert [node.service for node in chain] == ["redis", "order-service", "api-gateway"]
        assert chain[0].role == "root"
        assert chain[1].role == "affected"
        assert chain[2].role == "affected"
        assert chain[0].position == 1
        assert chain[2].position == 3

    def test_node_without_evidence_is_excluded(self):
        """拓扑上有关系但没证据的服务，不许进链 —— 否则是拿拓扑编故事。"""
        evidence_list = [
            ev("redis", EvidenceType.LOG),
            ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=20)),
        ]
        chain = build_propagation_chain(
            root_service="redis", evidence_list=evidence_list, topology=topology())

        assert [n.service for n in chain] == ["redis", "order-service"]
        assert "api-gateway" not in [n.service for n in chain]

    def test_no_topology_yields_root_only(self):
        evidence_list = [
            ev("redis", EvidenceType.LOG),
            ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=20)),
        ]
        chain = build_propagation_chain(root_service="redis", evidence_list=evidence_list, topology=None)
        assert [n.service for n in chain] == ["redis"]

    def test_unrelated_service_never_enters_chain(self):
        evidence_list = [
            ev("redis", EvidenceType.LOG),
            ev("billing-api", EvidenceType.LOG, at=TS + timedelta(seconds=30)),
        ]
        chain = build_propagation_chain(
            root_service="redis", evidence_list=evidence_list, topology=topology())
        assert "billing-api" not in [n.service for n in chain]

    def test_empty_evidence_yields_empty_chain(self):
        chain = build_propagation_chain(root_service="redis", evidence_list=[], topology=topology())
        assert chain == []

    def test_zero_record_evidence_never_enters_the_chain(self):
        """零记录的证据**不是观测** —— 不能让服务进链。

        链上每一跳都声称"我们观测到了这个服务"。一条空证据会让
        "我们查过它"冒充"我们看见它出问题了"。
        """
        evidence_list = [
            ev("redis", EvidenceType.LOG, at=TS),
            zero_record_ev("order-service"),
            zero_record_ev("api-gateway"),
        ]
        chain = build_propagation_chain(
            root_service="redis", evidence_list=evidence_list, topology=topology())

        assert [n.service for n in chain] == ["redis"], (
            "只有零记录证据的服务不该进链")


class TestChainEvidenceLinks:
    def test_each_node_carries_its_evidence_ids(self):
        """链上每跳必须能点回证据 —— 这是"可追溯"的最低要求。"""
        redis_log = ev("redis", EvidenceType.LOG)
        order_log = ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=20))
        chain = build_propagation_chain(
            root_service="redis",
            evidence_list=[redis_log, order_log],
            topology=topology(),
        )

        assert chain[0].evidence_ids == [redis_log.id]
        assert chain[1].evidence_ids == [order_log.id]

    def test_node_records_first_observed_at(self):
        """时间先后的依据要留在链上，供人复核是否真的"先坏后传播"。"""
        early = ev("redis", EvidenceType.LOG, at=TS)
        late = ev("redis", EvidenceType.METRIC, at=TS + timedelta(seconds=60))
        chain = build_propagation_chain(
            root_service="redis", evidence_list=[late, early], topology=topology())

        assert chain[0].first_observed_at == TS
        assert len(chain[0].evidence_ids) == 2


class TestChainOrdering:
    def test_order_follows_topological_distance(self):
        """链顺序按拓扑距离，不按证据到达顺序（证据顺序噪声大）。"""
        evidence_list = [
            ev("api-gateway", EvidenceType.TRACE, at=TS),  # 时间上更早
            ev("redis", EvidenceType.LOG, at=TS + timedelta(seconds=60)),
            ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=30)),
        ]
        chain = build_propagation_chain(
            root_service="redis", evidence_list=evidence_list, topology=topology())

        assert [n.service for n in chain] == ["redis", "order-service", "api-gateway"]

    def test_order_is_deterministic_for_same_depth(self):
        t = ServiceTopology.from_edges([
            ("api-gateway", "order-service"),
            ("order-service", "redis"),
            ("worker-queue", "redis"),
        ])
        evidence_list = [
            ev("redis", EvidenceType.LOG),
            ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=10)),
            ev("worker-queue", EvidenceType.LOG, at=TS + timedelta(seconds=20)),
        ]
        first = build_propagation_chain(root_service="redis", evidence_list=evidence_list, topology=t)
        second = build_propagation_chain(
            root_service="redis", evidence_list=list(reversed(evidence_list)), topology=t)
        assert [n.service for n in first] == [n.service for n in second]

    def test_timeline_violation_is_flagged_not_silently_accepted(self):
        """症状比根因先出现 = 时间线可疑（时钟漂移或标签错）。要标记，不要假装没看见。"""
        evidence_list = [
            ev("api-gateway", EvidenceType.TRACE, at=TS - timedelta(seconds=30)),
            ev("redis", EvidenceType.LOG, at=TS),
        ]
        chain = build_propagation_chain(
            root_service="redis", evidence_list=evidence_list, topology=topology())

        assert chain[1].observed_after_predecessor is False
        assert chain[0].observed_after_predecessor is None  # 根因没有前驱


class TestChainSummary:
    def test_chain_summary_is_readable(self):
        evidence_list = [
            ev("redis", EvidenceType.LOG, at=TS),
            ev("order-service", EvidenceType.LOG, at=TS + timedelta(seconds=20)),
        ]
        chain = build_propagation_chain(
            root_service="redis", evidence_list=evidence_list, topology=topology())
        text = " -> ".join(n.service for n in chain)
        assert text == "redis -> order-service"

    def test_node_is_serialisable(self):
        node = PropagationNode(
            service="redis", role="root", position=1, evidence_ids=["e-1"],
            first_observed_at=TS, observed_after_predecessor=None,
        )
        dumped = node.to_dict()
        assert dumped["service"] == "redis"
        assert dumped["role"] == "root"
        assert dumped["position"] == 1
        assert dumped["evidence_ids"] == ["e-1"]
