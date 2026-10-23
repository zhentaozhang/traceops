"""渲染层只搬运、不推理。

这些用例守的是同一件事：**人读的那段文字必须能在报告对象里找到出处。**
一旦渲染开始"补一句解释"，审计看 JSON、人看文字，两者就会分叉。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.cli.demo import DemoAdapters, build_request
from traceops.domain.models import EvidenceType, RCAReport
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import RunContext, run_investigation
from traceops.rca.render import render_hypotheses, render_report
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

SECTIONS = ("结论", "为什么信", "下一步（建议，不是结论）", "不确定的地方", "成本")


def _topology() -> ServiceTopology:
    return ServiceTopology.from_edges(
        [
            ("checkout-service", "payment-service"),
            ("checkout-service", "redis"),
            ("payment-service", "redis"),
        ]
    )


def _run(adapters=None):
    ctx = RunContext(
        store=EvidenceStore(),
        adapters=adapters or DemoAdapters(),
        budget=BudgetManager(max_tool_calls=40),
        topology=_topology(),
    )
    return run_investigation(build_request(), ctx)


@pytest.fixture(scope="module")
def report() -> RCAReport:
    result = _run()
    assert result.report is not None
    return result.report


class TestTheFourSections:
    def test_every_section_is_present(self, report):
        text = render_report(report, cost={"tool_calls": 9, "tokens_used": 0})
        for section in SECTIONS:
            assert section in text

    def test_conclusion_names_the_rank_one_candidate_in_plain_words(self, report):
        text = render_report(report)
        assert "已确认" in text
        # 机制换成能读懂的一句话，而不是那句面向审计的英文自由文本
        assert "连接池耗尽" in text
        assert "redis" in text

    def test_conclusion_says_where_the_time_came_from(self, report):
        text = render_report(report)
        assert "最早异常时间" in text
        assert "观测到的，不是告警声称的" in text


class TestWhyItBelievesIt:
    def test_each_supporting_evidence_carries_its_own_number(self, report):
        text = render_report(report)
        assert "命中机制关键词" in text        # 日志那一行
        assert "最慢" in text and "ms" in text  # 追踪那一行

    def test_no_raw_payload_or_checksum_leaks_into_the_reading(self, report):
        text = render_report(report)
        assert "checksum" not in text
        assert "normalized_data" not in text
        assert "trace_id" not in text

    def test_no_candidates_is_stated_once_and_not_padded(self):
        empty = RCAReport(
            investigation_run_id="run-x",
            diagnosis_status="INCONCLUSIVE",
            summary="no candidates",
        )
        text = render_report(empty)
        assert "没有产生任何根因候选" in text
        # 没有候选时不该出现"没有支持证据"——那是另一件事（有候选但没证据）
        assert "没有支持证据" not in text

    def test_a_hypothesis_with_no_supporting_links_says_so(self):
        from traceops.domain.models import RootCauseHypothesis

        report = RCAReport(
            investigation_run_id="run-x",
            diagnosis_status="INCONCLUSIVE",
            summary="one candidate, nothing supports it",
            hypotheses=[
                RootCauseHypothesis(
                    investigation_run_id="run-x",
                    candidate_service="order-service",
                    fault_type="latency_degradation",
                    failure_mechanism="m",
                    predicted_observations=[],
                    rank=1,
                )
            ],
        )
        text = render_report(report)
        assert "没有支持证据" in text


class TestNextStepsAreAdviceNotFindings:
    def test_steps_are_labelled_as_advice(self, report):
        text = render_report(report)
        assert "下一步（建议，不是结论）" in text

    def test_the_pool_advice_matches_the_mechanism(self, report):
        text = render_report(report)
        assert "连接池上限" in text

    def test_a_change_earlier_than_the_first_anomaly_is_pushed_to_the_front(self, report):
        text = render_report(report)
        assert "先看这次变更" in text
        # 变更的证据来自真实 EVENT 证据，不是按 fault_type 猜的
        assert "pool size 50 -> 20" in text


class TestUncertaintyKeepsTheThreeDirectionsApart:
    def test_unsuccessful_and_empty_queries_are_not_merged(self, report):
        text = render_report(report)
        assert "没查到" in text or "查了但没有" in text
        # 阴性观测必须单独成句：它和"没法查"的结论方向相反
        assert "查了但没有" in text

    def test_empty_queries_are_capped_and_counted(self, report):
        text = render_report(report)
        if len(report.no_data) > 3:
            assert "另有" in text

    def test_a_degraded_read_must_carry_where_to_read_instead(self):
        report = RCAReport(
            investigation_run_id="run-x",
            diagnosis_status="INCONCLUSIVE",
            summary="partial",
            degraded_reads=[
                {
                    "kind": "spill_unreadable",
                    "tool": "query_service_logs",
                    "read": "requery:query_service_logs with a narrower window",
                }
            ],
        )
        text = render_report(report)
        assert "降级读取" in text
        assert "requery:query_service_logs" in text

    def test_a_clean_run_says_so_rather_than_printing_an_empty_header(self, report):
        clean = report.model_copy(update={"missing_data": [], "no_data": [], "degraded_reads": []})
        text = render_report(clean)
        assert "不确定的地方" in text
        assert "无：所有计划内查询都成功了" in text

    def test_a_report_with_no_evidence_does_not_claim_the_queries_succeeded(self):
        report = RCAReport(
            investigation_run_id="run-x",
            diagnosis_status="INCONCLUSIVE",
            summary="nothing was recorded",
        )
        text = render_report(report)
        assert "无从判断取证是否完整" in text
        assert "所有计划内查询都成功了" not in text


class TestCostLine:
    def test_no_model_shows_as_no_model(self, report):
        text = render_report(report, cost={"tool_calls": 9, "tokens_used": 0})
        assert "未使用模型" in text

    def test_evidence_and_candidate_counts_come_from_the_report(self, report):
        text = render_report(report)
        assert f"证据 {len(report.evidence)} 条" in text
        assert f"候选 {len(report.hypotheses)} 个" in text


class TestCandidateList:
    def test_candidates_are_listed_by_rank_with_their_status(self, report):
        text = render_hypotheses(report)
        assert "#1" in text
        assert "支持" in text or "不确定" in text

    def test_no_candidates_is_stated(self):
        empty = RCAReport(
            investigation_run_id="run-x", diagnosis_status="INCONCLUSIVE", summary=""
        )
        assert "无" in render_hypotheses(empty)


class TestAnInconclusiveRunReadsAsInconclusive:
    def test_empty_datastores_produce_an_honest_abstention(self):
        class _Nothing:
            capabilities = frozenset({"metric_series", "error_rate", "logs", "traces"})

            def query_metric_series(self, *, service, metric, start, end):
                return {"status": "success", "data": {"resultType": "matrix", "result": []}}

            query_service_error_rate = query_metric_series

            def query_service_logs(self, *, service, start, end, filter=None):
                return {"status": "success", "data": {"resultType": "streams", "result": []}}

            def query_service_traces(self, *, service, start, end, min_duration_ms=None):
                return {"traces_considered": 0, "batches": []}

        result = _run(_Nothing())
        assert result.report is not None
        assert result.report.diagnosis_status == "INCONCLUSIVE"
        text = render_report(result.report)
        assert "无法确定" in text
        assert "查了但没有" in text  # 阴性观测被保留下来，而不是被静默吞掉
