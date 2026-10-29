"""数据源清单：能查什么、缺了什么，都必须是声明出来的。

这些用例守的是三个具体后果，不是一个数据结构：

1. 声明里**没有**的源不该进计划 —— 否则每次调查都多一条 missing_data，
   报告说"数据缺失"，真相是"这个部署没有这个源"；
2. 缺源的说法要能说清"少了这类证据结论还成不成立" —— 缺日志和缺剖析
   不是一回事；
3. 适配器没声明能力表时（测试替身、老代码）**不许**把"不知道"当成"什么都没有"，
   否则所有人都突然查不到任何东西。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from traceops.domain.models import EvidenceType
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest,
    RunContext,
    _capability_planned,
    _datasource_gaps,
    _plan_items,
    _report_missing_sources,
)
from traceops.runtime.budget import BudgetManager
from traceops.tools.gateway import ToolGateway, register_builtin_tools
from traceops.tools.manifest import (
    DatasourceManifest,
    ManifestError,
    SourceSpec,
    default_manifest,
    load_manifest,
    manifest_from_dict,
    present_capabilities,
)

FAULT = datetime(2026, 3, 12, 14, 32, tzinfo=UTC)


def _request(**overrides) -> InvestigationRequest:
    payload = {
        "incident_id": "inc-1",
        "investigation_run_id": "run-1",
        "service_hint": "checkout-service",
        "fault_start": FAULT,
        "window_start": FAULT - timedelta(minutes=10),
        "window_end": FAULT + timedelta(minutes=5),
        "alert_summary": "checkout p99 3.1s",
    }
    payload.update(overrides)
    return InvestigationRequest(**payload)


class _AllSourceAdapters:
    """四类都能查（演示栈的形状）。"""

    capabilities = frozenset({"metric_series", "error_rate", "logs", "traces", "profile"})

    def query_service_error_rate(self, *, service, start, end):
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    def query_service_logs(self, *, service, start, end, filter=None):
        return {"streams": []}

    def query_service_traces(self, *, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}

    def query_service_profile(self, *, service, start, end):
        return {"flamegraph": {"names": [], "values": []}}


class _MetricOnlyAdapters:
    """只有 Prometheus 的部署。"""

    capabilities = frozenset({"metric_series", "error_rate"})

    def query_service_error_rate(self, *, service, start, end):
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}


def _ctx(adapters, manifest=None) -> RunContext:
    return RunContext(
        store=EvidenceStore(),
        adapters=adapters,
        budget=BudgetManager(max_tool_calls=40, max_rounds=10),
        manifest=manifest,
    )


class TestManifestDeclaresWhatCanBeQueried:
    def test_default_manifest_is_the_demo_stack(self):
        manifest = default_manifest()
        assert manifest.names() == ("prometheus", "loki", "tempo", "pyroscope")
        assert manifest.supports("metric") and manifest.supports("log")
        assert manifest.supports("trace") and manifest.supports("profile")
        # 剖析是唯一可选的：缺了它结论照旧成立，只是看不到热点函数。
        assert [spec.name for spec in manifest.sources.values() if not spec.required] == ["pyroscope"]

    def test_a_source_must_say_what_the_report_claims_when_it_is_missing(self):
        with pytest.raises(ManifestError, match="when_absent"):
            SourceSpec(name="loki", capability="log", when_absent="  ")

    def test_unknown_capability_is_refused(self):
        with pytest.raises(ManifestError, match="unknown capability"):
            SourceSpec(name="loki", capability="logz", when_absent="x")

    def test_key_must_match_the_source_name(self):
        with pytest.raises(ManifestError, match="does not match"):
            DatasourceManifest(sources={"loki": SourceSpec(
                name="other", capability="log", when_absent="x")})

    def test_unknown_field_in_the_file_is_refused_with_the_known_ones(self):
        payload = {"sources": {"loki": {
            "capability": "log", "when_absent": "x", "precondition": ["typo"]}}}
        with pytest.raises(ManifestError) as exc:
            manifest_from_dict(payload)
        assert "preconditions" in str(exc.value)

    def test_a_file_declaring_only_metric_means_only_metric_is_queried(self):
        manifest = manifest_from_dict({"sources": {"prometheus": {
            "capability": "metric", "when_absent": "指标源缺失"}}})
        assert manifest.supports("metric")
        assert not manifest.supports("log") and not manifest.supports("trace")
        assert manifest.capabilities_in_use() == ("metric",)

    def test_preconditions_are_carried_for_the_reader(self):
        manifest = load_manifest(_write({
            "sources": {"loki": {
                "capability": "log",
                "preconditions": ["日志带 service 与 level 标签"],
                "when_absent": "日志源缺失",
            }}}))
        assert manifest.get("loki").preconditions == ("日志带 service 与 level 标签",)


class TestPlanningFollowsTheManifest:
    def test_default_manifest_plans_all_three_evidence_kinds(self):
        ctx = _ctx(_AllSourceAdapters())
        tools = [item["tool"] for item in _plan_items(_request(), ctx)]
        assert tools.count("query_service_error_rate") == 1
        assert tools.count("query_service_logs") == 1
        assert tools.count("query_service_traces") == 1

    def test_a_metric_only_deployment_does_not_plan_log_or_trace_queries(self):
        manifest = manifest_from_dict({"sources": {"prometheus": {
            "capability": "metric", "when_absent": "指标源缺失"}}})
        ctx = _ctx(_MetricOnlyAdapters(), manifest)
        tools = [item["tool"] for item in _plan_items(_request(), ctx)]
        assert tools == ["query_service_error_rate"]

    def test_a_source_the_manifest_declares_but_the_adapters_lack_is_skipped(self):
        # 清单说"我有 loki"，但适配器只有指标能力 —— 查必然失败，
        # 失败会被记成 missing_data，看起来像数据缺失。
        ctx = _ctx(_MetricOnlyAdapters(), default_manifest())
        assert _capability_planned(ctx, "log") is False
        assert _capability_planned(ctx, "metric") is True

    def test_adapters_that_declare_no_capabilities_are_not_assumed_empty(self):
        class _Undeclared:
            def query_service_error_rate(self, *, service, start, end):
                return {}

        ctx = _ctx(_Undeclared())
        assert present_capabilities(ctx.adapters) is None
        assert _capability_planned(ctx, "log") is True
        assert _capability_planned(ctx, "metric") is True

    def test_profile_is_planned_only_when_declared_and_available(self):
        ctx = _ctx(_AllSourceAdapters())
        assert _capability_planned(ctx, "profile") is True
        no_profile = manifest_from_dict({"sources": {"prometheus": {
            "capability": "metric", "when_absent": "x"}}})
        assert _capability_planned(_ctx(_AllSourceAdapters(), no_profile), "profile") is False


class TestToolRegistrationFollowsTheManifest:
    def test_default_registration_keeps_every_builtin_tool(self):
        gw = ToolGateway()
        register_builtin_tools(gw, _AllSourceAdapters())
        assert set(gw.tool_names()) >= {
            "query_metric_series", "query_service_error_rate",
            "query_service_logs", "query_service_traces", "query_service_profile",
        }

    def test_a_declared_manifest_hides_the_tools_it_does_not_declare(self):
        manifest = manifest_from_dict({"sources": {"prometheus": {
            "capability": "metric", "when_absent": "x"}}})
        gw = ToolGateway()
        register_builtin_tools(gw, _AllSourceAdapters(), manifest)
        assert set(gw.tool_names()) == {"query_metric_series", "query_service_error_rate"}


class TestGapsReachTheReport:
    def test_a_missing_required_source_is_named_with_its_consequence(self):
        ctx = _ctx(_MetricOnlyAdapters())
        gaps = _datasource_gaps(ctx)
        by_source = {gap["source"]: gap for gap in gaps}
        assert set(by_source) == {"loki", "tempo", "pyroscope"}
        assert by_source["loki"]["required"] is True
        assert by_source["pyroscope"]["required"] is False
        assert "日志源缺失" in by_source["loki"]["when_absent"]

    def test_only_required_gaps_enter_missing_data(self):
        ctx = _ctx(_MetricOnlyAdapters())
        missing: list[str] = []
        _report_missing_sources(ctx, missing)
        joined = " | ".join(missing)
        assert "loki" in joined and "tempo" in joined
        assert "pyroscope" not in joined  # 可选源不该稀释真正的缺失

    def test_a_complete_deployment_has_no_gaps_and_no_missing_lines(self):
        ctx = _ctx(_AllSourceAdapters())
        assert _datasource_gaps(ctx) == []
        missing: list[str] = []
        _report_missing_sources(ctx, missing)
        assert missing == []

    def test_undeclared_adapter_capabilities_produce_no_fake_gaps(self):
        class _Undeclared:
            pass

        # 不知道就等于"不缺"：把"不知道"印成"缺了"，报告会凭空多出一条失败。
        assert _datasource_gaps(_ctx(_Undeclared())) == []


class TestUndeclaredSourcesAreStatedOutLoud:
    def test_a_deployment_with_no_log_source_says_log_evidence_cannot_exist(self):
        manifest = manifest_from_dict({"sources": {"prometheus": {
            "capability": "metric", "when_absent": "指标源缺失"}}})
        missing: list[str] = []
        _report_missing_sources(_ctx(_MetricOnlyAdapters(), manifest), missing)
        joined = " | ".join(missing)
        assert "declares no log source" in joined
        assert "declares no trace source" in joined
        # 这是"部署事实"，不是"这次没查到"，措辞必须分得开。
        assert "a deployment fact, not a finding" in joined

    def test_a_complete_manifest_says_nothing_extra(self):
        missing: list[str] = []
        _report_missing_sources(_ctx(_AllSourceAdapters()), missing)
        assert missing == []

    def test_profile_is_not_a_baseline_capability(self):
        # 缺剖析不影响结论成立，所以它不该产生"没有这类证据"的声明。
        manifest = manifest_from_dict({"sources": {
            "prometheus": {"capability": "metric", "when_absent": "x"},
            "loki": {"capability": "log", "when_absent": "x"},
            "tempo": {"capability": "trace", "when_absent": "x"},
        }})
        assert manifest.absent_baseline_capabilities() == ()


class TestTheParseIsHonest:
    def test_non_object_manifest_is_refused(self):
        with pytest.raises(ManifestError):
            manifest_from_dict([])

    def test_sources_must_be_an_object(self):
        with pytest.raises(ManifestError, match="sources"):
            manifest_from_dict({"sources": []})

    def test_preconditions_must_be_strings(self):
        with pytest.raises(ManifestError, match="preconditions"):
            manifest_from_dict({"sources": {"loki": {
                "capability": "log", "when_absent": "x", "preconditions": [1]}}})

    def test_missing_capability_is_refused(self):
        with pytest.raises(ManifestError, match="unknown capability"):
            manifest_from_dict({"sources": {"loki": {"when_absent": "x"}}})


def _write(payload: dict) -> Path:
    import tempfile

    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    with handle:
        json.dump(payload, handle)
    return Path(handle.name)


def test_manifest_can_be_loaded_from_a_file(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"sources": {"tempo": {
        "capability": "trace", "when_absent": "trace 源缺失"}}}), encoding="utf-8")
    manifest = load_manifest(path)
    assert manifest.names() == ("tempo",)
    assert manifest.by_capability("trace")[0].name == "tempo"
    assert EvidenceType.TRACE.value == "trace"
