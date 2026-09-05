"""确定性矛盾检测：让 CONTRADICTS 关系真的会生成。

## 为什么这是"补功能"而不是"加设计"

`ranking._evaluate` 的 R-005 读 `contradicting_evidence_count`，
`RelationType.CONTRADICTS` 在领域模型里存在，但**没有任何代码生成它**。
结果是 R-005 成了死代码，`Contradiction Detection Rate` 分母恒为 0
（`评测口径` 自己承认这一点）。

## 为什么要确定性版本

aurora 的 C1 思想是「Design queries to disprove your hypothesis」。
我们受查询模板约束不能自由设计查询，但可以**在已采证据上做反证判定**——
判据全部可枚举，不需要 LLM，因此可复现、可解释。

## 铁律：没数据 ≠ 反证

最危险的误判是把"没看见"当成"看见反证"。所以三个 predicate 都要求
**确实有观测**（序列非空 / 日志有行 / span 有记录）才可能返回 True。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import (
    Evidence, EvidenceType, RootCauseHypothesis,
)
from traceops.rca.contradiction import find_contradictions

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _evidence(evidence_type: EvidenceType, normalized: dict, *, service="redis",
              template="qt-service-error-rate-v1") -> Evidence:
    return Evidence(
        incident_id="inc-1", investigation_run_id="run-1",
        evidence_type=evidence_type, source_id="src", service_id=service,
        observed_at=TS, window_start=TS - timedelta(minutes=2),
        window_end=TS + timedelta(minutes=3),
        query_template_id=template, query_params={},
        normalized_data=normalized, checksum="",
    )


def _hypothesis(fault_type: str, service="redis") -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="run-1", candidate_service=service,
        fault_type=fault_type, failure_mechanism="m", predicted_observations=[],
    )


class TestMetricContradiction:
    def test_normal_error_rate_contradicts_availability(self):
        """错误率全程低于阈值 → 推翻'可用性下降'假设。"""
        evidence = _evidence(EvidenceType.METRIC, {
            "kind": "metric", "result_type": "matrix",
            "series": [{"metric": {}, "values": [[1.0, "0.01"], [2.0, "0.02"]]}],
            "result_count": 1, "total_records": 1,
        })
        found = find_contradictions([_hypothesis("availability_degradation")], [evidence])
        assert len(found) == 1
        assert "below threshold" in found[0].reason

    def test_high_error_rate_is_not_a_contradiction(self):
        """错误率确实高 → 不构成反证（它支持假设）。"""
        evidence = _evidence(EvidenceType.METRIC, {
            "kind": "metric", "result_type": "matrix",
            "series": [{"metric": {}, "values": [[1.0, "0.42"]]}],
            "result_count": 1, "total_records": 1,
        })
        assert find_contradictions([_hypothesis("availability_degradation")], [evidence]) == []

    def test_empty_series_is_not_reassuring(self):
        """空序列 = 没数据，**不是**"正常"。绝不能当成反证。"""
        evidence = _evidence(EvidenceType.METRIC, {
            "kind": "metric", "result_type": "matrix",
            "series": [], "result_count": 0, "total_records": 0,
        })
        assert find_contradictions([_hypothesis("availability_degradation")], [evidence]) == []


class TestLogContradiction:
    def test_clean_logs_contradict_pool_exhaustion(self):
        evidence = _evidence(EvidenceType.LOG, {
            "kind": "log", "total_records": 2, "records": [
                {"timestamp": TS.isoformat(), "message": "request ok, latency normal",
                 "labels": {"level": "info"}},
                {"timestamp": TS.isoformat(), "message": "cache hit rate 99%",
                 "labels": {"level": "info"}},
            ],
        }, template="qt-service-logs-v1")
        found = find_contradictions([_hypothesis("pool_exhaustion")], [evidence])
        assert found and "no exhaustion" in found[0].reason

    def test_logs_with_errors_do_not_contradict(self):
        evidence = _evidence(EvidenceType.LOG, {
            "kind": "log", "total_records": 1, "records": [
                {"timestamp": TS.isoformat(), "message": "ERROR timeout", "labels": {}}],
        }, template="qt-service-logs-v1")
        assert find_contradictions([_hypothesis("pool_exhaustion")], [evidence]) == []

    def test_empty_logs_are_not_reassuring(self):
        evidence = _evidence(EvidenceType.LOG, {
            "kind": "log", "total_records": 0, "records": [],
        }, template="qt-service-logs-v1")
        assert find_contradictions([_hypothesis("pool_exhaustion")], [evidence]) == []


class TestTraceContradiction:
    def test_fast_spans_contradict_latency_degradation(self):
        evidence = _evidence(EvidenceType.TRACE, {
            "kind": "trace", "total_records": 2, "spans": [
                {"trace_id": "t", "span_id": "s", "service": "redis",
                 "operation_name": "redis.command", "duration_us": 5_000, "error": False},
            ],
        }, template="qt-slow-traces-v1")
        found = find_contradictions([_hypothesis("latency_degradation")], [evidence])
        assert found and "below the slow threshold" in found[0].reason

    def test_slow_spans_do_not_contradict(self):
        evidence = _evidence(EvidenceType.TRACE, {
            "kind": "trace", "total_records": 1, "spans": [
                {"trace_id": "t", "span_id": "s", "service": "redis",
                 "operation_name": "redis.command", "duration_us": 2_100_000, "error": True},
            ],
        }, template="qt-slow-traces-v1")
        assert find_contradictions([_hypothesis("latency_degradation")], [evidence]) == []


class TestAttributionMatters:
    def test_evidence_about_another_service_is_not_a_contradiction(self):
        """证据属于面试服务，不能用来推翻本服务的假设。"""
        evidence = _evidence(EvidenceType.METRIC, {
            "kind": "metric", "result_type": "matrix",
            "series": [{"metric": {}, "values": [[1.0, "0.01"]]}],
            "result_count": 1, "total_records": 1,
        }, service="mysql")
        found = find_contradictions([_hypothesis("availability_degradation", "redis")], [evidence])
        assert found == []

    def test_unknown_fault_type_yields_nothing(self):
        evidence = _evidence(EvidenceType.METRIC, {
            "kind": "metric", "result_type": "matrix",
            "series": [{"metric": {}, "values": [[1.0, "0.01"]]}],
            "result_count": 1, "total_records": 1,
        })
        assert find_contradictions([_hypothesis("some_novel_fault")], [evidence]) == []
