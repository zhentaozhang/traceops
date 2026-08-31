"""场景 rubric：把「规则自证」变成可证伪的。

## 为什么需要这个

现有 F-001~F-005 是**按检测规则反向构造**的：日志里写了 `redis timeout`，
`logs_are_anomalous` 就靠这个关键词命中。于是 AC@1=1.00 只证明了
"规则能认出它自己造的样本"，没有证明任何泛化能力。

aurora-lab 的做法值得直接借鉴（references/aurora-lab/scenarios/*/scenario.yaml）：
每个场景除了 `root_cause`，还显式声明

- `must_identify[]`：报告**必须**点到的东西（不止一个根因服务）
- `wrong_answers[]`：**看起来合理但实际错误**的答案，被系统说出口就算扣分

`wrong_answers` 是最关键的资产：把「Memory leak」「Node memory pressure」这类
系统性误判显式列出来，才能在评分时抓到"方向错但听起来专业"的输出。

这条指标能在 P0（无 LLM）阶段就回答一个此前只能靠文档断言的问题：
**现在的 1.00 到底是能力，还是场景太干净？**
"""
from __future__ import annotations

import pytest

from evaluation.metrics import ScenarioOutcome, score_scenario
from traceops.orchestration.graph import InvestigationResult
from evaluation.scenario import GroundTruth, Scenario, Signals


def _scenario(**gt_kwargs) -> Scenario:
    return Scenario(
        scenario_id="T-RUBRIC",
        name="rubric probe",
        service_hint="order-service",
        fault_start="2026-10-30T10:02:00Z",
        window_start="2026-10-30T10:00:00Z",
        window_end="2026-10-30T10:06:00Z",
        signals=Signals(error_logs=["ERROR redis timeout"]),
        ground_truth=GroundTruth(
            service="redis",
            fault_type="pool_exhaustion",
            expected_diagnosis="SUPPORTED",
            **gt_kwargs,
        ),
    )


def _wrap(report) -> InvestigationResult:
    """把裸报告包成 InvestigationResult —— score_scenario 的真实输入形状。"""
    return InvestigationResult(
        status=report.diagnosis_status, report=report, evidence_ids=[],
        errors=[], tool_calls=3, tokens_used=0,
    )


class TestRubricFieldsAreOptional:
    """不破坏现有 6 个场景：不写 rubric 时行为与现在完全一致。"""

    def test_scenario_without_rubric_still_valid(self):
        scenario = _scenario()
        assert scenario.ground_truth.must_identify == []
        assert scenario.ground_truth.wrong_answers == []

    def test_outcome_without_rubric_has_empty_scores(self):
        outcome = ScenarioOutcome(
            scenario_id="X", service_ac_at_1=True, service_ac_at_3=True,
            service_ac_at_5=True, fault_type_at_1=True, component_at_1=True,
            diagnosis_correct=True, abstained=False, evidence_recall=1.0,
            citation_precision=1.0, cited_evidence=3, fault_injected=True,
            chain_declared=False, chain_order_correct=None,
            affected_services_correct=None, tool_calls=3, tokens_used=0,
            missing_identifications=(), wrong_answers_asserted=(),
        )
        assert outcome.wrong_answers_asserted == ()


class TestWrongAnswersAreDetected:
    """被系统说出口的错误答案必须被抓到，否则「方向错但合理」无法量化。"""

    def test_declared_wrong_answer_is_flagged(self):
        """报告里出现 wrong_answers 列出的说法 → wrong_answers_asserted 非空。"""

        class _Report:
            diagnosis_status = "SUPPORTED"
            summary = "Root cause is a memory leak in order-service."
            hypotheses = []
            evidence = []
            propagation_chain = []
            affected_services = ["order-service"]
            missing_data = []
            abstained = False

            def all_evidence_ids(self):
                return set()

        scenario = _scenario(wrong_answers=["memory leak"])
        outcome = score_scenario(scenario, _wrap(_Report()))
        assert outcome.wrong_answers_asserted == ("memory leak",)

    def test_clean_report_asserts_no_wrong_answer(self):
        class _Report:
            diagnosis_status = "SUPPORTED"
            summary = "Root cause: redis connection pool exhaustion."
            hypotheses = []
            evidence = []
            propagation_chain = []
            affected_services = ["redis"]
            missing_data = []
            abstained = False

            def all_evidence_ids(self):
                return set()

        scenario = _scenario(wrong_answers=["memory leak", "node memory pressure"])
        outcome = score_scenario(scenario, _wrap(_Report()))
        assert outcome.wrong_answers_asserted == ()

    def test_matching_is_case_insensitive(self):
        """大小写不该影响判定：Memory Leak 与 memory leak 是同一件事。"""

        class _Report:
            diagnosis_status = "SUPPORTED"
            summary = "This is a MEMORY LEAK."
            hypotheses = []
            evidence = []
            propagation_chain = []
            affected_services = []
            missing_data = []
            abstained = False

            def all_evidence_ids(self):
                return set()

        scenario = _scenario(wrong_answers=["memory leak"])
        assert score_scenario(scenario, _wrap(_Report())).wrong_answers_asserted == ("memory leak",)


class TestMustIdentifyIsTracked:
    def test_missing_identification_is_reported(self):
        """must_identify 里有、但报告没提到的条目要能列出来。"""

        class _Report:
            diagnosis_status = "SUPPORTED"
            summary = "redis pool exhausted"
            hypotheses = []
            evidence = []
            propagation_chain = []
            affected_services = ["redis"]
            missing_data = []
            abstained = False

            def all_evidence_ids(self):
                return set()

        scenario = _scenario(must_identify=["redis", "api-gateway"])
        outcome = score_scenario(scenario, _wrap(_Report()))
        assert "api-gateway" in outcome.missing_identifications


class TestAggregateExposesRubricSignal:
    """聚合层必须把 rubric 结果暴露成比率，否则 runner 报告里看不到。"""

    def test_aggregate_has_wrong_answer_rate(self):
        from evaluation.metrics import aggregate

        outcomes = [
            ScenarioOutcome(
                scenario_id="A", service_ac_at_1=True, service_ac_at_3=True,
                service_ac_at_5=True, fault_type_at_1=True, component_at_1=True,
                diagnosis_correct=True, abstained=False, evidence_recall=1.0,
                citation_precision=1.0, cited_evidence=2, fault_injected=True,
                chain_declared=False, chain_order_correct=None,
                affected_services_correct=None, tool_calls=3, tokens_used=0,
                missing_identifications=(), wrong_answers_asserted=("memory leak",),
                rubric_declared=True,
            )
        ]
        agg = aggregate(outcomes)
        assert agg.wrong_answer_rate == pytest.approx(1.0)


def test_duplicate_field_is_gone():
    """`chain_declared` 曾在一个 dataclass 里声明两次（后者覆盖前者）。

    这是无害的（值相同），但它说明这个结构是手工拼的、没有测试盯住字段集合。
    """
    import dataclasses

    names = [f.name for f in dataclasses.fields(ScenarioOutcome)]
    assert len(names) == len(set(names)), f"duplicate fields: {names}"


class TestMetricIsNotMuted:
    """反例测试：指标必须在**真的出错时**报警，否则它就是被修哑了。

    这是 aurora-lab E-9 的直接教训——「评分器 bug 把产品能力判错」
    的镜像：如果我只是不断放宽匹配逻辑让 wrong_answer_rate 变 0，
    那就是把一个有用的报警器关掉，然后宣布"没问题"。

    所以这里构造一个**确实把症状服务断言为根因**的报告（SUPPORTED 状态），
    要求指标必须抓到。
    """

    def test_supported_hypothesis_naming_wrong_service_is_flagged(self):
        from traceops.domain.models import HypothesisStatus, RootCauseHypothesis

        class _Report:
            diagnosis_status = "SUPPORTED"
            summary = "Root cause: order-service availability degradation"
            evidence = []
            propagation_chain = []
            affected_services = []
            missing_data = []
            abstained = False

            def __init__(self):
                # 关键：status = SUPPORTED，即真的断言它为根因
                self.hypotheses = [
                    RootCauseHypothesis(
                        investigation_run_id="r", candidate_service="order-service",
                        fault_type="availability_degradation",
                        failure_mechanism="error rate crossed baseline",
                        predicted_observations=[],
                        status=HypothesisStatus.SUPPORTED,
                    )
                ]

            def all_evidence_ids(self):
                return set()

        scenario = _scenario(wrong_answers=["order-service"])
        outcome = score_scenario(scenario, _wrap(_Report()))
        assert outcome.wrong_answers_asserted == ("order-service",), (
            "metric is muted: a SUPPORTED hypothesis naming an unacceptable root cause "
            "must be flagged")
