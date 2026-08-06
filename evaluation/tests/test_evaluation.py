"""评估 harness 测试（Cycle C）。

口径（REVISIONS-1.1 R8）：
- AC@k 只衡量"服务定位"，k=1/3/5 分开统计
- Fault Type / Component 单独统计，不与服务定位混算
- Evidence Recall 与 Citation Precision 分开
- Abstention 单独统计（INCONCLUSIVE 是合法交付物）
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluation.metrics import AggregateMetrics, ScenarioOutcome, aggregate, score_scenario
from evaluation.scenario import Scenario, load_scenario
from evaluation.harness import run_scenario


SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


def _scenario(**overrides) -> Scenario:
    payload = {
        "scenario_id": "F-001",
        "name": "Redis pool exhaustion after config change",
        "service_hint": "order-service",
        "fault_start": "2026-10-30T10:02:00Z",
        "window_start": "2026-10-30T10:00:00Z",
        "window_end": "2026-10-30T10:05:00Z",
        "signals": {
            "error_rate_series": [0.01, 0.42],
            "error_logs": ["ERROR redis timeout after 2000ms: pool exhausted"],
            "slow_spans": [{"name": "redis.command", "duration_ms": 2000}],
            "config_change": {
                "changed_at": "2026-10-30T10:01:00Z",
                "component": "redis-connection-pool",
                "summary": "maxPoolSize reduced 20 -> 2",
            },
        },
        "ground_truth": {
            # 日志里写的是 "redis timeout / pool exhausted"，报告者是 order-service。
            # 归因规则：被点名的依赖才是根因服务。
            "service": "redis",
            "component": "redis-connection-pool",
            "fault_type": "pool_exhaustion",
            "expected_diagnosis": "SUPPORTED",
        },
    }
    payload.update(overrides)
    return Scenario.model_validate(payload)


def test_scenario_validates_timestamps() -> None:
    with pytest.raises(ValueError):
        _scenario(window_start="2026-10-30T10:10:00Z", window_end="2026-10-30T10:00:00Z")


def test_run_scenario_produces_outcome_with_metrics() -> None:
    scenario = _scenario()

    outcome = run_scenario(scenario)

    assert outcome.scenario_id == "F-001"
    assert outcome.service_ac_at_1 is True
    assert outcome.fault_type_at_1 is True
    assert outcome.component_at_1 is True
    assert outcome.diagnosis_correct is True
    assert outcome.abstained is False
    assert outcome.evidence_recall == 1.0
    assert 0.0 < outcome.citation_precision <= 1.0
    assert outcome.tool_calls == 3  # 三个只读工具（事件证据不调工具）


def test_no_signal_scenario_abstains_and_scores_zero_ac() -> None:
    scenario = _scenario(
        scenario_id="F-000",
        name="healthy control",
        signals={},
        ground_truth={
            "service": "order-service",
            "component": None,
            "fault_type": "none",
            "expected_diagnosis": "INCONCLUSIVE",
        },
    )

    outcome = run_scenario(scenario)

    assert outcome.abstained is True
    assert outcome.diagnosis_correct is True
    assert outcome.service_ac_at_1 is False
    assert outcome.evidence_recall == 0.0


def test_aggregate_reports_separate_metric_families() -> None:
    good = run_scenario(_scenario())
    control = run_scenario(
        _scenario(
            scenario_id="F-000",
            name="healthy control",
            signals={},
            ground_truth={
                "service": "order-service",
                "component": None,
                "fault_type": "none",
                "expected_diagnosis": "INCONCLUSIVE",
            },
        )
    )

    agg = aggregate([good, control])

    assert isinstance(agg, AggregateMetrics)
    assert agg.scenario_count == 2
    assert 0.0 < agg.ac_at_1 <= 1.0
    assert agg.abstention_rate == 0.5
    # 对照组没有注入故障，不进入 AC@k / 证据指标的分母
    assert agg.evidence_recall_mean == 1.0
    assert agg.fault_type_accuracy == 1.0
    assert agg.ac_at_1 == 1.0


def test_load_scenario_from_json_file(tmp_path) -> None:
    raw = _scenario().model_dump(mode="json")
    path = tmp_path / "F-001.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    scenario = load_scenario(path)

    assert scenario.scenario_id == "F-001"
    assert scenario.ground_truth.fault_type == "pool_exhaustion"


def test_shipped_scenarios_are_valid_and_cover_five_faults() -> None:
    files = sorted(SCENARIOS_DIR.glob("F-*.json"))
    assert len(files) >= 5, f"expected at least 5 scenario files, found {len(files)}"
    scenarios = [load_scenario(p) for p in files]
    fault_types = {s.ground_truth.fault_type for s in scenarios}
    assert {"pool_exhaustion", "dependency_timeout", "latency_degradation", "availability_degradation"} <= fault_types
    for s in scenarios:
        assert s.fault_start.tzinfo is not None


class TestMultiServiceChain:
    """F-005 级联场景：多服务采集 + 传播链/影响范围校验。"""

    def _cascade(self) -> Scenario:
        return load_scenario(SCENARIOS_DIR / "F-005.json")

    def test_cascade_scenario_declares_topology(self):
        scenario = self._cascade()
        assert scenario.topology_edges, "cascade scenario must declare topology"
        assert len(scenario.signals.per_service) >= 3

    def test_cascade_identifies_downstream_root_not_upstream_symptom(self):
        scenario = self._cascade()

        outcome = run_scenario(scenario)

        # 根因是 redis（最下游），api-gateway 只是症状
        assert outcome.service_ac_at_1 is True
        assert outcome.fault_type_at_1 is True

    def test_cascade_reports_affected_upstream_services(self):
        scenario = self._cascade()
        outcome = run_scenario(scenario)

        # 影响范围来自拓扑 blast radius，不靠 LLM 猜
        assert outcome.affected_services_correct is True

    def test_cascade_span_offset_encodes_symptom_lag(self):
        """症状必须晚于根因：span_offset_ms 让 trace 时间戳递增。"""
        scenario = self._cascade()
        offsets = {
            name: signals.span_offset_ms
            for name, signals in scenario.signals.per_service.items()
            if signals.slow_spans
        }
        assert offsets["redis"] < offsets["order-service"] < offsets["api-gateway"]

    def test_multi_service_citation_precision_is_lownot_perfect(self):
        """多服务采集的代价：非根因服务的证据会被引用，precision 必然低于 1。

        这是诚实性测试 —— 如果这里变成 1.00，说明指标在撒谎。
        """
        outcome = run_scenario(self._cascade())
        assert outcome.citation_precision < 0.5, outcome.citation_precision
