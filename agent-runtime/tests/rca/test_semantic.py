"""语义校验：把「预期观测的覆盖率」变成可算的东西。

这是六类校验里唯一一直空着的一类（蓝图 §6.5 Semantic Validator）。

## 为什么它可以是确定性的

PRD 把它标成"需人工标注数据"，但那只说对了一半。每个故障机制都有
**可枚举的预期观测**：

  pool_exhaustion        → 错误率上升 + 错误日志 + 慢 span（三件套）
  latency_degradation    → 慢 span（且不该有错误日志）
  availability_degradation → 错误率上升 + 错误日志

于是"这个假设声称的观测，实际命中了几项"是可算的。剩下真正需要 LLM 的
才只有"证据的语义内容是否真的指向该机制"——那一半继续不做。

## 为什么这比"有没有支持证据"更强

现在 SUPPORTED 只看 supporting_evidence_count > 0：一条含 timeout 的日志
就能让任意假设通过。覆盖率能回答更精确的问题：**它声称的那组观测命中了几项**。

aurora 的 conclusion_gate 说「Absence of expected evidence is evidence」——
预期证据的缺失本身是证据。覆盖率正是这个意思。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import Evidence, EvidenceType, RootCauseHypothesis
from traceops.rca.semantic import check_all, check_hypothesis

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _ev(evidence_type, normalized, *, service="redis", template="qt-x"):
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=evidence_type,
        source_id="s", service_id=service, observed_at=TS,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id=template, query_params={},
        normalized_data=normalized, checksum="",
    )


def _hyp(fault_type, service="redis"):
    return RootCauseHypothesis(
        investigation_run_id="r", candidate_service=service, fault_type=fault_type,
        failure_mechanism="m", predicted_observations=[],
    )


def _metric(value="0.42"):
    return _ev(EvidenceType.METRIC, {
        "kind": "metric", "result_type": "matrix",
        "series": [{"metric": {}, "values": [[TS.timestamp(), value]]}],
        "result_count": 1, "total_records": 1})


def _log(message="ERROR redis timeout", level="error"):
    return _ev(EvidenceType.LOG, {
        "kind": "log", "total_records": 1,
        "records": [{"timestamp": TS.isoformat(), "message": message,
                     "labels": {"level": level}}]}, template="qt-service-logs-v1")


def _slow_trace():
    return _ev(EvidenceType.TRACE, {
        "kind": "trace", "total_records": 1,
        "spans": [{"trace_id": "t", "span_id": "s", "service": "redis",
                   "operation_name": "redis.command", "duration_us": 2_100_000,
                   "error": True}]}, template="qt-slow-traces-v1")


class TestCoverage:
    def test_full_evidence_set_gives_full_coverage(self):
        """池耗尽的三件套全齐 → 覆盖率 1.0。"""
        check = check_hypothesis(_hyp("pool_exhaustion"), [_metric(), _log(), _slow_trace()])
        assert check.expected == ("error_rate_rise", "error_logs", "slow_span")
        assert check.coverage == 1.0
        assert check.missing == ()

    def test_partial_evidence_is_partial_coverage(self):
        """只有日志 → 命中 1/3，且缺失项要能列出来。"""
        check = check_hypothesis(_hyp("pool_exhaustion"), [_log()])
        assert check.coverage == round(1 / 3, 4)
        assert "error_rate_rise" in check.missing
        assert "slow_span" in check.missing

    def test_no_evidence_gives_zero_coverage(self):
        check = check_hypothesis(_hyp("pool_exhaustion"), [])
        assert check.coverage == 0.0

    def test_unknown_fault_type_has_no_expectations(self):
        """没有预期清单的机制不能被"算"成覆盖率 0 当作失败——它是无法校验。"""
        check = check_hypothesis(_hyp("some_novel_fault"), [_metric()])
        assert check.expected == ()
        assert check.coverage == 0.0


class TestLatencyMechanismDistinction:
    def test_latency_needs_only_slow_span(self):
        check = check_hypothesis(_hyp("latency_degradation"), [_slow_trace()])
        assert check.coverage == 1.0

    def test_slow_span_does_not_cover_availability(self):
        """慢 span 不是"可用性下降"的预期观测 —— 机制之间不能互相顶替。"""
        check = check_hypothesis(_hyp("availability_degradation"), [_slow_trace()])
        assert check.coverage == 0.0
        assert "error_rate_rise" in check.missing


class TestAttributionBoundary:
    def test_evidence_about_other_service_does_not_count(self):
        """别的服务的证据不能用来满足本服务假设的预期观测。"""
        foreign = [_ev(EvidenceType.METRIC, {
            "kind": "metric", "result_type": "matrix",
            "series": [{"metric": {}, "values": [[TS.timestamp(), "0.42"]]}],
            "result_count": 1, "total_records": 1}, service="mysql")]
        check = check_hypothesis(_hyp("availability_degradation", "redis"), foreign)
        assert check.coverage == 0.0


class TestCheckAll:
    def test_returns_one_check_per_hypothesis(self):
        hypotheses = [_hyp("pool_exhaustion"), _hyp("latency_degradation")]
        checks = check_all(hypotheses, [_metric(), _log(), _slow_trace()])
        assert len(checks) == 2
        assert {c.hypothesis_id for c in checks} == {h.id for h in hypotheses}


class TestSemanticGateInReport:
    """覆盖率 0 时，SUPPORTED 必须降级 —— 否则这个校验只是记录，不是闸门。"""

    def _build(self, *, fault_type, evidence_list):
        from traceops.domain.models import HypothesisStatus
        from traceops.rca.ranking import RankedCandidate
        from traceops.rca.report import build_report
        from traceops.rca.semantic import check_hypothesis
        from traceops.evidence.store import EvidenceStore

        hypothesis = _hyp(fault_type)
        store = EvidenceStore()
        for evidence in evidence_list:
            store.add(evidence)
        ranked = [RankedCandidate(hypothesis_id=hypothesis.id, score=0, rank=1,
                                  status=HypothesisStatus.SUPPORTED)]
        checks = {hypothesis.id: check_hypothesis(hypothesis, evidence_list)}
        return build_report(
            investigation_run_id="r", hypotheses=[hypothesis], links=[],
            evidence=evidence_list, ranked=ranked, fault_start=TS, store=store,
            incident_id="i", semantic_checks=checks,
        )

    def test_zero_coverage_downgrades_to_inconclusive(self):
        """声称 latency_degradation，却只有错误日志（没有任何慢 span）→ 降级。"""
        report = self._build(fault_type="latency_degradation", evidence_list=[_log()])
        assert report.diagnosis_status == "INCONCLUSIVE"
        assert "expected" in report.summary.lower()

    def test_partial_coverage_stays_supported(self):
        """部分覆盖是正常的：真实故障不必凑齐全部预期观测。"""
        report = self._build(fault_type="pool_exhaustion",
                             evidence_list=[_log(), _slow_trace()])
        assert report.diagnosis_status == "SUPPORTED"

    def test_unknown_mechanism_is_not_gated(self):
        """没有预期清单的机制无法校验，不能因此被降级。"""
        report = self._build(fault_type="some_novel_fault", evidence_list=[_log()])
        assert report.diagnosis_status == "SUPPORTED"
