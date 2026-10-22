"""变更事件（部署/配置/开关/扩缩容）作为一等证据：因果分界线必须用"首个异常"。

## 被替换的缺陷

R-003 的判据是「变更事件早于 `fault_start`」，而 `fault_start` 来自告警的
`startsAt` —— 告警通常**晚于**真正的起变点：监控要先攒够窗口、要过阈值、
还要走通知链路。于是"故障已经开始、告警还没发出"这段时间里发生的部署
被判成"与故障无关"，而那恰好是部署最容易背锅的位置。

这条规则以前只在"单个 config_change、单个服务、窗口与告警对齐"的
场景里看起来对，因为它测的是**告警时间**而不是**故障时间**。

## 修法

分界线改成「首个异常观测」（`earliest_anomaly_at`），没有该信息时退回
`fault_start`：宁可少给分，不可凭感觉给分。

## 为什么一次可以有多条变更

真实事故里"变了什么"很少只有一件：一次发版往往同时动配置、同时扩缩容。
只收单条 `config_change` 会逼着调用方挑一条，而被丢掉的那条可能才是原因。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import (
    CreatedBy,
    Evidence,
    EvidenceType,
    HypothesisEvidence,
    RelationType,
    RootCauseHypothesis,
)
from traceops.evidence.store import EvidenceStore
from traceops.rca.ranking import derive_signals

ALERT_AT = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)  # 告警时刻 = 旧代码的 fault_start
CHANGE_AT = ALERT_AT + timedelta(seconds=40)  # 故障已开始、告警刚发出之后
FIRST_ANOMALY = ALERT_AT + timedelta(seconds=90)  # 首个异常观测
WINDOW_START = ALERT_AT - timedelta(minutes=10)  # 变更常常发生在告警之前很久
WINDOW_END = ALERT_AT + timedelta(minutes=5)


def _change_event(store: EvidenceStore, *, at: datetime,
                  change_kind: str = "deployment") -> Evidence:
    return store.add(
        Evidence(
            incident_id="inc-1",
            investigation_run_id="run-1",
            evidence_type=EvidenceType.EVENT,
            source_id="control-plane",
            service_id="checkout-service",
            observed_at=at,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            query_template_id="qt-deployment-v1",
            query_params={},
            normalized_data={
                "kind": "event",
                "events": [{"type": change_kind, "service": "checkout-service",
                            "revision": "r42", "timestamp": at.isoformat()}],
                "total_records": 1,
                "truncated": False,
            },
            checksum="",
        )
    )


def _metric_anomaly(store: EvidenceStore, *, at: datetime) -> Evidence:
    return store.add(
        Evidence(
            incident_id="inc-1",
            investigation_run_id="run-1",
            evidence_type=EvidenceType.METRIC,
            source_id="prometheus",
            service_id="checkout-service",
            observed_at=at,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            query_template_id="qt-error-rate-v1",
            query_params={},
            normalized_data={
                "kind": "metric",
                "result_type": "matrix",
                "series": [{"metric": {}, "values": [[at.timestamp(), "0.42"]]}],
                "result_count": 1,
                "total_records": 1,
            },
            checksum="",
        )
    )


def _hypothesis() -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="run-1",
        candidate_service="checkout-service",
        fault_type="availability_degradation",
        failure_mechanism="new revision raised the error rate",
        predicted_observations=[],
    )


def _link(hypothesis_id: str, evidence_id: str) -> HypothesisEvidence:
    return HypothesisEvidence(
        hypothesis_id=hypothesis_id,
        evidence_id=evidence_id,
        relation_type=RelationType.SUPPORTS,
        reason="linked by rule",
        created_by=CreatedBy.RULE,
    )


def _signals(*, change_at: datetime, earliest_anomaly_at: datetime | None):
    store = EvidenceStore()
    hypothesis = _hypothesis()
    change = _change_event(store, at=change_at)
    metric = _metric_anomaly(store, at=FIRST_ANOMALY)
    links = [_link(hypothesis.id, change.id), _link(hypothesis.id, metric.id)]
    return derive_signals(
        hypothesis,
        links,
        store,
        fault_start=ALERT_AT,
        earliest_anomaly_at=earliest_anomaly_at,
    )


class TestTheCausalCutoffIsTheFirstAnomaly:
    def test_a_change_after_the_alert_but_before_the_first_anomaly_still_counts(self):
        # 部署发生在告警之后 40 秒，但早于首个异常 —— 这就是"部署背锅"的典型位置
        signals = _signals(change_at=CHANGE_AT, earliest_anomaly_at=FIRST_ANOMALY)

        assert signals.config_change_precedes_fault is True

    def test_the_same_change_is_not_causal_without_the_first_anomaly(self):
        # 没有最早异常信息时退回 fault_start：这条部署在告警之后，于是不因果。
        # 保守方向是对的 —— 少给分比凭感觉给分安全。
        signals = _signals(change_at=CHANGE_AT, earliest_anomaly_at=None)

        assert signals.config_change_precedes_fault is False

    def test_a_change_after_the_first_anomaly_is_not_causal(self):
        # 异常先出现、变更后发生 → 变更不可能是原因（这条以前也成立，防回归）
        signals = _signals(
            change_at=FIRST_ANOMALY + timedelta(seconds=30),
            earliest_anomaly_at=FIRST_ANOMALY,
        )

        assert signals.config_change_precedes_fault is False

    def test_a_change_before_the_alert_still_counts(self):
        signals = _signals(
            change_at=ALERT_AT - timedelta(minutes=5),
            earliest_anomaly_at=FIRST_ANOMALY,
        )

        assert signals.config_change_precedes_fault is True
