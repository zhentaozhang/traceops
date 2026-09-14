"""多服务场景的 `expected_evidence_types` 漏掉了 `per_service` 里的信号。

## 缺陷

```python
def expected_evidence_types(self):
    if self.error_rate_series:  types.add(METRIC)
    if self.error_logs:         types.add(LOG)
    if self.slow_spans:         types.add(TRACE)
```

只看**顶层**字段。多服务场景把信号写在 `per_service` 里（F-005/F-006/F-008/F-009
都是这种形状），顶层字段全为空 → `expected` 集合为空 → `evidence_recall`
的分母为 0 → 恒返回 0.0。

`evidence_recall` 是"应找到的证据类型被实际引用的比例"。分母恒为 0 时
它对所有多服务场景都是废指标，而多服务正是最需要它的场景。

F-009 实测暴露：场景声明了 metric + slow_span，recall 报 0.00 ——
不是"没找到证据"，是"分母算错了"。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evaluation.scenario import Scenario, Signals, ServiceSignals, GroundTruth
from traceops.domain.models import EvidenceType

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _scenario(signals: Signals) -> Scenario:
    return Scenario(
        scenario_id="T", name="t", service_hint="order-service",
        fault_start=TS, window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3), signals=signals,
        ground_truth=GroundTruth(service="redis", fault_type="pool_exhaustion"),
    )


class TestPerServiceSignalsCount:
    def test_per_service_logs_are_expected(self):
        signals = Signals(per_service={
            "redis": ServiceSignals(error_logs=["ERROR pool exhausted"])})
        assert EvidenceType.LOG in _scenario(signals).signals.expected_evidence_types()

    def test_per_service_metric_is_expected(self):
        signals = Signals(per_service={
            "redis": ServiceSignals(error_rate_series=[0.01, 0.4])})
        assert EvidenceType.METRIC in _scenario(signals).signals.expected_evidence_types()

    def test_per_service_slow_span_is_expected(self):
        signals = Signals(per_service={
            "redis": ServiceSignals(slow_spans=[{"name": "redis.command", "duration_ms": 2000}])})
        assert EvidenceType.TRACE in _scenario(signals).signals.expected_evidence_types()

    def test_multiple_services_union(self):
        """多个服务各自的信号类型应当取并集。"""
        signals = Signals(per_service={
            "redis": ServiceSignals(error_logs=["ERROR x"]),
            "order-service": ServiceSignals(error_rate_series=[0.01, 0.4]),
        })
        types = _scenario(signals).signals.expected_evidence_types()
        assert EvidenceType.LOG in types
        assert EvidenceType.METRIC in types


class TestTopLevelStillWorks:
    def test_single_service_shape_unchanged(self):
        signals = Signals(error_rate_series=[0.01, 0.4], error_logs=["ERROR x"])
        types = _scenario(signals).signals.expected_evidence_types()
        assert EvidenceType.METRIC in types and EvidenceType.LOG in types

    def test_empty_signals_expect_nothing(self):
        assert _scenario(Signals()).signals.expected_evidence_types() == set()
