"""报告必须能回答「根因**什么时候**开始的」。

## 缺口（与 OpenRCA 对比时发现）

OpenRCA 的答案是**三元素**：`root cause occurrence datetime` / `component` / `reason`，
时间容差 ±60s。

我们的 `RCAReport` 有 11 个字段，**没有根因发生时间** ——
`fault_start` 只是场景/请求输入，既不进报告、也不进指标。

后果：

- 对外答不出"什么时候开始的"，答案粒度比 OpenRCA 低一档
- `citation_precision` / `evidence_recall` 都有，但**时间准确度**无从度量
- 运维拿到报告还得自己去图上找时间点

## 正确做法：时间来自**证据**，不是来自请求

不能直接把请求里的 `fault_start` 抄进报告 —— 那是**假设**（告警声称的时间），
不是**观测**。报告里的时间必须来自能支持根因的证据：

- 取 rank1 候选**支持证据**里最早的观测时间（`observed_at`）
- 没有支持证据时**留空**并标注，不要拿 fault_start 充数

这个区分是本项目"证据可信"原则的直接应用：假设的时间不是事实。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import (
    Evidence, EvidenceType, HypothesisStatus, RootCauseHypothesis,
)
from traceops.evidence.store import EvidenceStore
from traceops.rca.ranking import RankedCandidate
from traceops.rca.report import build_report


TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
LATER = TS + timedelta(minutes=3)


def _evidence(observed: datetime, service: str = "redis") -> Evidence:
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.LOG,
        source_id="loki", service_id=service, observed_at=observed,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id="qt", query_params={},
        normalized_data={"kind": "log", "total_records": 1,
                         "records": [{"timestamp": observed.isoformat(),
                                      "message": "ERROR redis timeout", "labels": {}}]},
        checksum="")


def _hypothesis() -> RootCauseHypothesis:
    return RootCauseHypothesis(
        investigation_run_id="r", candidate_service="redis",
        fault_type="availability_degradation",
        failure_mechanism="m", predicted_observations=["error_logs"])


class TestReportCarriesRootCauseTime:
    def test_report_has_a_root_cause_time_field(self):
        hypothesis = _hypothesis()
        log = _evidence(TS)
        store = EvidenceStore()
        store.add(log)
        report = build_report(
            investigation_run_id="r", hypotheses=[hypothesis], links=[],
            evidence=[log], ranked=[RankedCandidate(
                hypothesis_id=hypothesis.id, score=3, rank=1,
                status=HypothesisStatus.SUPPORTED)],
            fault_start=TS, store=store, incident_id="i")
        assert hasattr(report, "root_cause_time"), (
            "report must expose when the root cause began")

    def test_time_comes_from_supporting_evidence_not_the_request(self):
        """时间必须来自证据的 observed_at，而不是请求里的 fault_start。

        这是刻意的：fault_start 是告警**声称**的时间，observed_at 是
        我们**实际观测到**的时间。报告只能用后者。
        """
        from traceops.domain.models import HypothesisEvidence, RelationType

        hypothesis = _hypothesis()
        early, later = _evidence(LATER), _evidence(TS, service="order-service")
        store = EvidenceStore()
        store.add(early)
        store.add(later)
        # 两条都是支持证据：时间应取最早的 TS（来自 later 这条证据的 observed_at）
        links = [
            HypothesisEvidence(hypothesis_id=hypothesis.id, evidence_id=early.id,
                               relation_type=RelationType.SUPPORTS, reason="r",
                               created_by="rule"),
            HypothesisEvidence(hypothesis_id=hypothesis.id, evidence_id=later.id,
                               relation_type=RelationType.SUPPORTS, reason="r",
                               created_by="rule"),
        ]
        report = build_report(
            investigation_run_id="r", hypotheses=[hypothesis],
            links=links, evidence=[early, later],
            ranked=[RankedCandidate(hypothesis_id=hypothesis.id, score=3, rank=1,
                                    status=HypothesisStatus.SUPPORTED)],
            fault_start=LATER, store=store, incident_id="i")
        # 最早的支持证据是 TS（10:02），而 fault_start 是 LATER（10:05）
        assert report.root_cause_time == TS, (
            f"expected earliest evidence time {TS}, got {report.root_cause_time}")

    def test_no_evidence_means_no_time_rather_than_a_guess(self):
        """没有支持证据时必须留空，不能拿 fault_start 充数。"""
        hypothesis = _hypothesis()
        report = build_report(
            investigation_run_id="r", hypotheses=[hypothesis], links=[],
            evidence=[], ranked=[RankedCandidate(
                hypothesis_id=hypothesis.id, score=0, rank=1,
                status=HypothesisStatus.INCONCLUSIVE)],
            fault_start=TS, store=EvidenceStore(), incident_id="i")
        assert report.root_cause_time is None, (
            "an uninvestigated incident must not invent a root cause time")
