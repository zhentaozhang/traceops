"""三个在真实数据上暴露出来的排序缺陷。

背景（RCAEval RE1-OB，124 个真实案例，`评测口径` §6.7 有复现命令）：
第一版 AC@1 只有 **0.54**，而"取指标峰值最高的服务"这条一行代码的基线有
**0.87**。拆开失败案例之后发现三个机制问题，这里逐个钉住：

- **R-011（幅度）**：排序只看布尔特征（"有没有异常观测"），于是幅度完全
  不参与判断。真实数据里 13 个服务中只有被注入那条曲线发生了阶跃 ——
  最有信息量的维度被丢掉了。
- **R-009（谁先坏）**：判据是 `own_earliest == global_earliest`。真实数据里
  所有序列都以同一时刻收尾，"最早"对所有人同时成立，于是这条规则变成给
  每个候选都加 1 分的常数，实际破平靠服务名字母序 —— 分数看起来用了时间，
  其实没用。
- **R-008（症状降级）**：没有调用图时我们给导入数据编了一个"以告警服务为
  中心的星形"拓扑。`ServiceTopology` 把 `(a, b)` 读作"a 依赖 b"，于是这条
  声明实际在说"告警服务依赖所有别的服务"，症状降级随即把真因判成下游的
  症状：52 个失败案例的真因都带 `R-008 (symptom of ...)`。

三处都是**机制**缺陷，不是数据集特性：换任何一份"序列以告警时刻收尾"的
真实数据都会重现。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import (
    Evidence, EvidenceType, HypothesisEvidence, HypothesisStatus, RelationType,
    RootCauseHypothesis,
)
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext, _scope_services
from traceops.rca.ranking import (
    CandidateSignals, RankedCandidate, derive_signals, rank_hypotheses,
)
from traceops.rca.report import build_report

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _hypothesis(service: str, fault_type: str = "availability_degradation") -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="r", candidate_service=service,
        fault_type=fault_type, failure_mechanism="m",
        predicted_observations=["error_rate_rise"])


def _metric(store: EvidenceStore, service: str, peak: float) -> Evidence:
    evidence = Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.METRIC,
        source_id="prometheus", service_id=service, observed_at=TS,
        window_start=TS - timedelta(minutes=30), window_end=TS,
        query_template_id="qt-error-rate-v1", query_params={},
        normalized_data={"kind": "metric", "series": [
            {"metric": f"{service}_errors", "values": [[TS.timestamp() - 60, 0.0],
                                                       [TS.timestamp(), peak]]},
        ]},
        checksum="")
    store.add(evidence)
    return evidence


def _link(hypothesis: RootCauseHypothesis, evidence: Evidence) -> HypothesisEvidence:
    return HypothesisEvidence(
        hypothesis_id=hypothesis.id, evidence_id=evidence.id,
        relation_type=RelationType.SUPPORTS, reason="test", created_by="rule")


def _signals(hid: str, *, severity: float = 0.0, earliest: bool = False) -> CandidateSignals:
    return CandidateSignals(
        hypothesis_id=hid, has_specific_component=False, has_anomaly_observation=True,
        supporting_evidence_count=1, severity=severity, is_earliest_anomaly=earliest,
    )


class TestSeverityBreaksTies:
    """R-011：规则分相同、观测幅度不同时，幅度大的排前面，并且**记**下来。"""

    def test_the_larger_deviation_wins_a_tie_and_says_so(self):
        a, b = _hypothesis("alpha"), _hypothesis("beta")
        ranked = rank_hypotheses([a, b], [
            _signals(a.id, severity=0.90), _signals(b.id, severity=0.10)])
        assert ranked[0].hypothesis_id == a.id
        assert any("R-011" in rule for rule in ranked[0].applied_rules), (
            "同分靠幅度取胜时必须留痕，否则报告里两个候选同分、一个排前面，"
            "读者无从判断为什么是它")

    def test_equal_severity_falls_back_without_the_rule(self):
        a, b = _hypothesis("alpha"), _hypothesis("beta")
        ranked = rank_hypotheses([a, b], [
            _signals(a.id, severity=0.5), _signals(b.id, severity=0.5)])
        assert ranked[0].hypothesis_id == a.id  # 服务名字母序（已知成本）
        assert not any("R-011" in rule for rule in ranked[0].applied_rules)

    def test_severity_does_not_override_a_real_evidence_advantage(self):
        """幅度只破平，不加分：证据更硬的候选不该被"更响"的候选顶掉。"""
        strong, loud = _hypothesis("alpha"), _hypothesis("beta")
        strong_signals = CandidateSignals(
            hypothesis_id=strong.id, has_specific_component=True,
            has_anomaly_observation=True, trace_anomaly=True,
            config_change_precedes_fault=True,
            supporting_evidence_count=4, severity=0.1)
        loud_signals = _signals(loud.id, severity=1.0)
        ranked = rank_hypotheses([strong, loud], [strong_signals, loud_signals])
        assert ranked[0].hypothesis_id == strong.id

    def test_severity_is_relative_to_one_shared_denominator(self):
        """候选幅度 = 自己的峰值 / 全池最大峰值。分母必须同一个。"""
        store = EvidenceStore()
        weak = _metric(store, "weak-service", 0.01)
        strong = _metric(store, "strong-service", 0.20)
        weak_h, strong_h = _hypothesis("weak-service"), _hypothesis("strong-service")
        scale = 0.20
        weak_signals = derive_signals(
            weak_h, [_link(weak_h, weak)], store, fault_start=TS, severity_scale=scale)
        strong_signals = derive_signals(
            strong_h, [_link(strong_h, strong)], store, fault_start=TS, severity_scale=scale)
        assert 0 < weak_signals.severity < strong_signals.severity <= 1.0
        assert strong_signals.severity == 1.0
        assert weak is not strong

    def test_without_a_denominator_severity_stays_zero(self):
        """没有指标数据时不参与这个裁决，而不是当成 0 幅度去输给别人。"""
        store = EvidenceStore()
        hypothesis = _hypothesis("alpha")
        evidence = _metric(store, "alpha", 0.20)
        signals = derive_signals(
            hypothesis, [_link(hypothesis, evidence)], store,
            fault_start=TS, severity_scale=0.0)
        assert signals.severity == 0.0


class TestEarliestAnomalyMustBeUnique:
    """R-009：只有**唯一**最早才算"它先坏"，并列即弃权。"""

    def _signals_for(self, store, service, *, earliest_at, count):
        hypothesis = _hypothesis(service)
        evidence = _metric(store, service, 0.2)
        return derive_signals(
            hypothesis, [_link(hypothesis, evidence)], store, fault_start=TS,
            earliest_anomaly_at=earliest_at,
            earliest_anomaly_count=count), evidence, hypothesis

    def test_a_tie_awards_nothing(self):
        store = EvidenceStore()
        signals, evidence, _ = self._signals_for(
            store, "alpha", earliest_at=evidence_time(), count=2)
        assert evidence.observed_at == evidence_time()
        assert signals.is_earliest_anomaly is False, (
            "两条证据同时最早时，把 +1 给所有人等于常数加分；"
            "真实数据上它只让分数看起来用了时间")

    def test_a_unique_earliest_earns_the_point(self):
        store = EvidenceStore()
        signals, _, _ = self._signals_for(
            store, "alpha", earliest_at=evidence_time(), count=1)
        assert signals.is_earliest_anomaly is True


def evidence_time() -> datetime:
    return TS


class TestScopeWithoutDirection:
    """范围与方向是两件事：可以知道"要查谁"，而不知道"谁调用谁"。"""

    def _request(self, **kwargs) -> InvestigationRequest:
        base = dict(
            investigation_run_id="r", incident_id="i", service_hint="checkoutservice",
            fault_start=TS, window_start=TS - timedelta(minutes=30), window_end=TS,
        )
        base.update(kwargs)
        return InvestigationRequest(**base)

    def _ctx(self) -> RunContext:
        from traceops.evidence.store import EvidenceStore
        return RunContext(store=EvidenceStore(), adapters=object(), topology=None)

    def test_declared_services_enter_the_scope_without_a_topology(self):
        request = self._request(scope_services=["paymentservice", "adservice"])
        services = [s for s, _tier in _scope_services(request, self._ctx())]
        assert services[0] == "checkoutservice"
        assert set(services) == {"checkoutservice", "paymentservice", "adservice"}

    def test_without_scope_services_only_the_alerting_service_is_investigated(self):
        services = [s for s, _tier in _scope_services(self._request(), self._ctx())]
        assert services == ["checkoutservice"]

    def test_no_topology_means_no_symptom_demotion(self):
        """范围清单不产生边，因此 R-008 保持关闭 —— 我们不知道谁在上游。"""
        truth = _hypothesis("checkoutservice")
        other = _hypothesis("emailservice")
        ranked = rank_hypotheses([truth, other], [
            _signals(truth.id, severity=0.9), _signals(other.id, severity=0.1)],
            topology=None)
        assert ranked[0].status is HypothesisStatus.SUPPORTED
        assert not any("R-008" in rule for rule in ranked[0].applied_rules)


class TestTheAmbiguityGateCanUseAmplitude:
    """同分但幅度差一个数量级时，"分不清谁是真因"不成立。"""

    def _report(self, severities: tuple[float, float]):
        a, b = _hypothesis("alpha"), _hypothesis("beta")
        store = EvidenceStore()
        _metric(store, "alpha", 0.2)
        _metric(store, "beta", 0.2)
        ranked = [
            RankedCandidate(hypothesis_id=a.id, score=3, rank=1,
                            status=HypothesisStatus.SUPPORTED, severity=severities[0]),
            RankedCandidate(hypothesis_id=b.id, score=3, rank=2,
                            status=HypothesisStatus.SUPPORTED, severity=severities[1]),
        ]
        return build_report(
            investigation_run_id="r", hypotheses=[a, b], links=[], evidence=[],
            ranked=ranked, fault_start=TS, store=store, incident_id="i")

    def test_materially_different_amplitudes_are_not_ambiguous(self):
        assert self._report((0.90, 0.05)).diagnosis_status == "SUPPORTED"

    def test_indistinguishable_amplitudes_still_abstain(self):
        assert self._report((0.90, 0.89)).diagnosis_status == "INCONCLUSIVE"
