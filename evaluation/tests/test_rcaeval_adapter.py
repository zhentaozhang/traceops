"""RCAEval 适配器：外部基准的数据必须变成**可诊断的 Evidence**。

## 为什么"不伪装成 Grafana 数据源"这条要求重要

蓝图 §10 明确：适配器把基准数据转成 TraceOps 观测格式，
**离线基准与真实诊断共用同一套 RCA Engine / Validator / Report Schema**。

如果适配器只吐出裸指标、绕过 Evidence 归一化与校验，那测出来的就不是
同一套系统 —— 基准分数再高也不代表线上能力。

所以本测试的核心断言是：适配器产出的东西**确实是 Evidence**，
字段完整（来源/时间/模板/查询），能直接进 Store 和调查流程。
"""
from __future__ import annotations

import pytest

from evaluation.adapters.rcaeval_adapter import (
    RcaEvalDataMissing, discover_cases, fault_type_for, load_case,
)
from traceops.domain.models import EvidenceType


@pytest.fixture
def case_dir(tmp_path):
    """按 RCAEval 真实布局造一个最小案例。

    布局：data/<dataset>/<service>_<metric>/<case>/data.csv
    """
    dataset = tmp_path / "data" / "RE2" / "RE2-OB"
    parent = dataset / "order-service_cpu"
    case = parent / "case-001"
    case.mkdir(parents=True)
    # 宽表：time + 每指标一列；含一个 NaN（RCAEval 不 forward-fill）
    (case / "data.csv").write_text(
        "time,order-service_cpu,redis_cpu\n"
        "1793300000,0.10,0.20\n"
        "1793300060,NaN,0.85\n"
        "1793300120,0.45,0.90\n",
        encoding="utf-8",
    )
    (case / "inject_time.txt").write_text("1793300060", encoding="utf-8")
    return case


class TestLoadCaseProducesEvidence:
    def test_evidence_is_real_evidence_objects(self, case_dir):
        result = load_case(case_dir, dataset="re2-ob")
        assert result.evidence, "adapter must emit evidence, not bare points"
        for item in result.evidence:
            assert item.evidence_type is EvidenceType.METRIC
            # 可追溯三件套：来源、模板、实际查询
            assert item.source_id == "rcaeval"
            assert item.query_template_id
            assert item.rendered_query
            assert item.observed_at is not None

    def test_ground_truth_comes_from_directory_name(self, case_dir):
        result = load_case(case_dir, dataset="re2-ob")
        assert result.service == "order-service"
        assert result.metric == "cpu"

    def test_inject_time_is_parsed(self, case_dir):
        result = load_case(case_dir, dataset="re2-ob")
        assert result.inject_time is not None
        assert result.inject_time.year == 2026

    def test_nan_is_skipped_not_zero_filled(self, case_dir):
        """RCAEval 明确不 forward-fill；把 NaN 当 0 会凭空造出"指标正常"的观测。"""
        result = load_case(case_dir, dataset="re2-ob")
        cpu_series = [e for e in result.evidence if e.service_id == "order-service"]
        assert cpu_series, "expected the order-service_cpu column"
        values = cpu_series[0].normalized_data["series"][0]["values"]
        assert len(values) == 2, f"NaN row must be skipped, got {values}"
        assert all(v[1] is not None for v in values)


class TestEvidenceIsDiagnosable:
    def test_goes_into_evidence_store_and_validates(self, case_dir):
        """证据必须能通过 Store 的 checksum 校验 —— 否则不是"可信证据"。"""
        from traceops.evidence.store import EvidenceStore

        result = load_case(case_dir, dataset="re2-ob")
        store = EvidenceStore()
        store.register_source("rcaeval")
        stored_ids = []
        for item in result.evidence:
            stored = store.add(item)
            stored_ids.append(stored.id)
        assert len(stored_ids) == 2, "two metric columns -> two evidence objects"

        for evidence_id in stored_ids:
            assert store.validate(evidence_id).passed, (
                f"adapter-produced evidence must pass checksum validation: {evidence_id}")

    def test_ground_truth_not_in_observations(self, case_dir):
        """防止数据泄漏：诊断入参里不能带 ground truth。"""
        result = load_case(case_dir, dataset="re2-ob")
        observations = result.to_observations()
        assert "ground_truth" not in observations
        assert "service" not in str(observations.get("labels", ""))


class TestMissingDataIsAnError:
    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(RcaEvalDataMissing, match="not found"):
            discover_cases(tmp_path / "nope")

    def test_missing_csv_raises(self, tmp_path):
        case = tmp_path / "data" / "x" / "svc_cpu" / "case"
        case.mkdir(parents=True)
        with pytest.raises(RcaEvalDataMissing, match="no data.csv"):
            load_case(case, dataset="x")

    def test_discover_finds_real_case_dirs(self, case_dir):
        cases = discover_cases(case_dir.parents[2])
        assert case_dir in cases


class TestVocabularyMapping:
    def test_known_metrics_map(self):
        assert fault_type_for("latency") == "latency_degradation"
        assert fault_type_for("cpu") == "availability_degradation"

    def test_unknown_metric_is_tolerated(self):
        """不替外部数据集校验词汇：未知 metric 仍要能产出可诊断观测。"""
        assert fault_type_for("some_new_metric") == "availability_degradation"


class TestAdapterDrivesRealInvestigation:
    """最重要的断言：适配器产物必须能**真的驱动调查**，而不只是形状对。

    蓝图 §10 要求基准测试与真实诊断共用同一套 RCA Engine。
    如果适配器的证据进不了 run_investigation，那"接入基准"就只是数据格式转换。
    """

    def test_evidence_feeds_run_investigation(self, case_dir):
        from datetime import timedelta

        from traceops.domain.models import EvidenceType
        from traceops.evidence.store import EvidenceStore
        from traceops.orchestration.graph import (
            InvestigationRequest, RunContext, run_investigation,
        )
        from traceops.runtime.budget import BudgetManager

        result = load_case(case_dir, dataset="re2-ob")
        store = EvidenceStore()
        store.register_source("rcaeval")
        for item in result.evidence:
            store.add(item)

        window_start = result.evidence[0].window_start
        window_end = result.evidence[0].window_end

        # 直接把 Evidence 塞进 store，调查读的是 store 里的证据。
        # 适配器不负责采集 —— 采集层在真实环境走数据源，离线走预置证据。
        from evaluation.adapters.rcaeval_adapter import EvidenceOnlyAdapters

        ctx = RunContext(
            store=store,
            adapters=EvidenceOnlyAdapters(result.evidence),
            budget=BudgetManager(max_tool_calls=40),
            topology=None,
        )
        investigation = run_investigation(
            InvestigationRequest(
                investigation_run_id="rcaeval-run", incident_id="rcaeval-inc",
                environment="benchmark", service_hint=result.service,
                fault_start=window_end,
                window_start=window_start, window_end=window_end,
            ), ctx)
        assert investigation.status != "FAILED", investigation.errors
        assert investigation.report is not None
        # 证据数量要能被报告引用（不是空跑）
        assert investigation.report.evidence or investigation.report.missing_data
