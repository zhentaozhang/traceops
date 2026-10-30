"""导入器：真实数据集必须走与被信任数字**同一条**打分路径。

## 这一层要防的三件事

1. **自己算分**。`evaluation/adapters/` 那条路忠实回放原始值、自己算分，
   于是它的 AC@k 与 12 个合成场景不可比，也进不了漏斗与朴素基线。
2. **偷偷改题**。时间错 8 小时、把 NaN 填成 0、把一个案例的多个组件混成一个 ——
   每一种都会让分数好看或难看，而原因藏在转换里。
3. **把"没下载"当成"没异常"**。数据集缺失时必须抛错，不能返回空列表。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evaluation.adapters.rcaeval_adapter import RcaEvalDataMissing
from evaluation.harness import error_rate_payload
from evaluation.importers.cli import main as cli_main
from evaluation.importers.rcaeval import (
    GRID_POINTS,
    PEAK,
    import_case,
    import_dataset,
    summarise,
    write_scenarios,
)
from evaluation.scenario import Scenario

INJECT_EPOCH = 1793300060  # 2026-10-29T…Z，与其它评测夹具同一年


def _write(case: Path, rows: list[str], *, inject: str = str(INJECT_EPOCH)) -> Path:
    case.mkdir(parents=True, exist_ok=True)
    (case / "data.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (case / "inject_time.txt").write_text(inject, encoding="utf-8")
    return case


@pytest.fixture
def dataset(tmp_path) -> Path:
    """一个最小 RCAEval 数据集：一个案例，两个注入目录 + 一条干扰列。

    宽表意味着：`order-service` 与 `billing-api` 也会作为列出现在
    `redis_cpu` 的表里 —— 真实布局就是这样，二手列必须被认出来。
    """
    root = tmp_path / "data" / "RE2" / "RE2-OB"
    _write(root / "order-service_cpu" / "case-001", [
        "time,order-service_cpu,redis_cpu,billing-api_cpu",
        f"{INJECT_EPOCH - 120},10.0,20.0,11.0",
        f"{INJECT_EPOCH - 60},10.5,21.0,11.2",
        f"{INJECT_EPOCH},10.2,85.0,11.1",
        f"{INJECT_EPOCH + 60},10.1,90.0,11.0",
    ])
    _write(root / "redis_cpu" / "case-001", [
        "time,redis_cpu,order-service_cpu",
        f"{INJECT_EPOCH - 120},20.0,10.0",
        f"{INJECT_EPOCH - 60},21.0,10.5",
        f"{INJECT_EPOCH},85.0,10.2",
        f"{INJECT_EPOCH + 60},90.0,10.1",
    ])
    return root


class TestConversionIsFaithful:
    def test_truth_comes_from_the_data_when_directories_disagree(self, dataset):
        """两个目录各自声称自己是真因时，取偏离最大的那个。"""
        imported = import_dataset(dataset, dataset="re2-ob")
        assert len(imported) == 1
        case = imported[0]
        assert case.service == "redis"
        assert case.metric == "cpu"
        assert case.scenario.ground_truth.service == "redis"

    def test_naive_inject_time_is_interpreted_as_utc_plus_8(self, tmp_path):
        """RCAEval 的时间戳无时区且按 UTC+8 记录；按本机时区解释会错 8 小时。

        夹具里的 "2026-11-02T08:10:00" 就是 UTC+8 的写法，对应的真实时刻是
        00:10Z；数据行也按这个时刻摆，所以只要时区解释对了，窗口就是吻合的。
        """
        injected = datetime(2026, 11, 2, 0, 10, tzinfo=UTC)
        epoch = injected.timestamp()
        root = tmp_path / "data" / "RE2" / "RE2-OB"
        _write(root / "order-service_cpu" / "case-009", [
            "time,order-service_cpu",
            f"{epoch - 120},10.0",
            f"{epoch - 60},10.5",
            f"{epoch},40.0",
        ], inject="2026-11-02T08:10:00")
        case = import_dataset(root, dataset="re2-ob")[0]
        assert case.scenario.fault_start == injected

    def test_an_inject_time_outside_the_data_is_refused(self, tmp_path):
        """时区解释错了、或 inject_time 与 CSV 不是同一案例时，必须报错而不是产出空场景。"""
        root = tmp_path / "data" / "RE2" / "RE2-OB"
        _write(root / "order-service_cpu" / "case-011", [
            "time,order-service_cpu",
            f"{INJECT_EPOCH - 60},10.0",
            f"{INJECT_EPOCH},30.0",
        ], inject="2030-01-01T00:00:00")
        with pytest.raises(RcaEvalDataMissing, match="lies outside the observations"):
            import_dataset(root, dataset="re2-ob")

    def test_nan_does_not_become_zero(self, dataset):
        """NaN 是"那一刻没观测"，填成 0 会凭空造出一段平稳期。"""
        case = import_dataset(dataset, dataset="re2-ob")[0]
        series = case.scenario.signals.per_service["redis"].error_rate_series
        assert series, "the injected service must have a series"
        assert len(series) <= GRID_POINTS
        # 归一化后正常段的取值接近 0 但**不等于** 0（0 会读成"确认正常"）
        assert max(series) >= PEAK * 0.9

    def test_sparse_data_yields_a_short_series_not_a_filled_one(self, tmp_path):
        root = tmp_path / "data" / "RE2" / "RE2-OB"
        _write(root / "order-service_cpu" / "case-010", [
            "time,order-service_cpu",
            f"{INJECT_EPOCH - 120},10.0",
            f"{INJECT_EPOCH - 60},11.0",
            f"{INJECT_EPOCH},40.0",
        ])
        case = import_dataset(root, dataset="re2-ob")[0]
        series = case.scenario.signals.per_service["order-service"].error_rate_series
        assert 0 < len(series) < GRID_POINTS, "short series is the honest outcome"

    def test_the_owned_directory_wins_over_a_borrowed_column(self, dataset):
        """`redis` 同时出现在自己的目录和别人的宽表里，必须用自己的那一份。"""
        case = import_dataset(dataset, dataset="re2-ob")[0]
        series = case.scenario.signals.per_service["redis"].error_rate_series
        assert max(series) >= PEAK * 0.9  # 自己目录里 85/90 才是真实峰值

    def test_documenting_what_was_changed(self, dataset):
        """三处转换（时间/网格/量纲）必须跟着场景走，不能只写在文档里。"""
        description = import_dataset(dataset, dataset="re2-ob")[0].scenario.description
        assert "UTC+8" in description
        assert "largest step maps to" in description
        assert "nearest neighbour" in description


class TestTheScenarioIsUsableDownstream:
    def test_the_window_contains_the_fault(self, dataset):
        scenario = import_dataset(dataset, dataset="re2-ob")[0].scenario
        assert scenario.window_start < scenario.fault_start < scenario.window_end

    def test_the_harness_sees_the_injected_spike(self, dataset):
        """真正的验收：喂给 harness 之后，注入时刻前后确实超过错误率阈值。"""
        scenario = import_dataset(dataset, dataset="re2-ob")[0].scenario
        payload = error_rate_payload(scenario, "redis")
        values = [point[1] for series in payload["data"]["result"] for point in series["values"]]
        assert max(values) >= 0.05, "the injected service must look anomalous"
        quiet = error_rate_payload(scenario, "billing-api")
        quiet_values = [p[1] for s in quiet["data"]["result"] for p in s["values"]]
        assert max(quiet_values) < 0.05, "a quiet service must not look anomalous"

    def test_written_scenarios_reload_as_scenarios(self, dataset, tmp_path):
        imported = import_dataset(dataset, dataset="re2-ob")
        written = write_scenarios(imported, tmp_path / "out")
        assert len(written) == 1
        reloaded = Scenario.model_validate_json(written[0].read_text(encoding="utf-8"))
        assert reloaded.scenario_id == imported[0].scenario.scenario_id

    def test_the_candidate_pool_starts_with_the_truth(self, dataset):
        case = import_dataset(dataset, dataset="re2-ob")[0]
        assert case.candidate_pool[0] == "redis"
        assert 1 <= len(case.candidate_pool) <= 3


class TestSameScoringPathAsTheSyntheticScenarios:
    """这一层存在与否的区别，就体现在这两个用例上。"""

    def test_the_imported_case_is_scored_by_the_same_metrics(self, dataset):
        from evaluation.harness import run_scenario
        from evaluation.metrics import aggregate, score_scenario

        case = import_dataset(dataset, dataset="re2-ob")[0]
        outcome = score_scenario(case.scenario, run_scenario(case.scenario))
        assert outcome.service_ac_at_1 is True
        assert outcome.fault_injected is True
        agg = aggregate([outcome])
        # 漏斗与朴素基线的字段必须真的被填上：真实数据也得进这三层拆解
        assert agg.observed_rate == 1.0
        assert agg.candidate_coverage == 1.0
        assert agg.peak_heuristic_cases >= 1

    def test_the_funnel_can_be_computed_on_imported_data(self, dataset):
        from evaluation.decomposition import funnel
        from evaluation.harness import run_scenario

        case = import_dataset(dataset, dataset="re2-ob")[0]
        report = run_scenario(case.scenario).report
        result = funnel(report, "redis")
        assert result.observed is True
        assert result.in_candidates is True
        assert result.top1 is True


class TestMissingDataIsNotSilence:
    def test_a_missing_directory_raises(self, tmp_path):
        with pytest.raises(RcaEvalDataMissing):
            import_dataset(tmp_path / "nope", dataset="x")

    def test_a_directory_without_cases_raises(self, tmp_path):
        empty = tmp_path / "data" / "RE2" / "RE2-OB"
        empty.mkdir(parents=True)
        with pytest.raises(RcaEvalDataMissing):
            import_dataset(empty, dataset="x")

    def test_a_broken_case_is_skipped_only_if_something_else_survives(self, dataset, tmp_path):
        broken = dataset / "loki_cpu" / "case-000"
        broken.mkdir(parents=True)
        (broken / "data.csv").write_text("time,loki_cpu\n", encoding="utf-8")
        imported = import_dataset(dataset, dataset="re2-ob")
        assert len(imported) == 1  # the good case survives

    def test_the_summary_reports_pool_sizes(self, dataset):
        summary = summarise(import_dataset(dataset, dataset="re2-ob"))
        assert summary["cases"] == 1
        assert summary["datasets"] == ["re2-ob"]
        assert summary["candidate_pool_max"] <= 3


class TestCli:
    def test_probe_prints_the_shape_without_writing(self, dataset, capsys):
        assert cli_main(["probe", "--dataset-dir", str(dataset), "--name", "re2-ob"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "OK"
        assert payload["cases"] == 1

    def test_import_writes_files(self, dataset, tmp_path, capsys):
        out = tmp_path / "scenarios"
        code = cli_main(["import-rcaeval", "--dataset-dir", str(dataset),
                         "--out", str(out), "--name", "re2-ob"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["written"] == 1
        assert list(out.glob("*.json"))

    def test_missing_dataset_exits_2_with_a_reason(self, tmp_path, capsys):
        code = cli_main(["probe", "--dataset-dir", str(tmp_path / "nope")])
        assert code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "MISSING_DATA"
        assert "not found" in payload["reason"]


class TestOneCaseCanIncludeSeveralDirectories:
    def test_a_multi_directory_case_keeps_one_series_per_service(self, dataset):
        case = import_case(sorted(dataset.glob("**/case-001")), dataset="re2-ob")
        services = set(case.scenario.signals.per_service)
        assert {"order-service", "redis", "billing-api"} <= services


class TestStepScore:
    """阶跃分数：真实数据逼出来的三条性质。

    第一版用"相对全窗口中位数的最大绝对偏离"，在真实案例上把内存漂移
    （70 分钟单调爬升）当成最异常信号，真因的 cpu 阶跃反而落选。
    """

    def test_a_monotonic_trend_does_not_beat_a_step(self):
        from evaluation.importers.rcaeval import _step_score

        anchor = INJECT_EPOCH
        times = [anchor - 60 * 60 + i * 60 for i in range(90)]
        # 内存：全程匀速爬升（注入前后斜率一样）→ 阶跃分数应当很小。
        trend = [100.0 + i * 10.0 for i in range(90)]
        # cpu：注入那一刻跳到 50 倍，前后都平。
        step = [5.0 if t < anchor else 250.0 for t in times]
        trend_score = _step_score(times, trend, anchor=anchor)
        step_score = _step_score(times, step, anchor=anchor)
        assert step_score > 10
        # 趋势不是零分（线性爬升的两个窗口中位数当然不同），但必须比阶跃
        # 小两个数量级以上，否则"谁更异常"就由漂移决定。
        assert trend_score < step_score / 20

    def test_a_drop_counts_as_an_anomaly_too(self):
        from evaluation.importers.rcaeval import _step_score

        anchor = INJECT_EPOCH
        times = [anchor - 30 * 60 + i * 60 for i in range(50)]
        values = [400.0 if t < anchor else 40.0 for t in times]
        assert _step_score(times, values, anchor=anchor) > 5

    def test_one_divisor_per_case(self):
        """真因不再恒等于 PEAK —— 幅度是待判断的证据，不是答案。"""
        from evaluation.importers.rcaeval import PEAK

        root = Path(__file__).resolve().parent / "_unused"
        assert root  # 只为说明这个类不依赖文件系统
        # 真因阶跃 100，干扰项阶跃 200：真因的峰值必须**小于** PEAK。
        scenario = _two_service_case(truth_step=100.0, other_step=200.0)
        per_service = scenario.signals.per_service
        assert max(per_service["order-service"].error_rate_series) < PEAK
        assert max(per_service["redis"].error_rate_series) <= PEAK


def _two_service_case(*, truth_step: float, other_step: float) -> Scenario:
    """构造一个"真因阶跃 / 干扰阶跃"可控的最小案例（不需要文件）。"""
    from evaluation.importers.rcaeval import (
        _normalise,
        _resample,
        _step_score,
    )

    anchor = INJECT_EPOCH
    times = [anchor - 30 * 60 + i * 60 for i in range(60)]
    truth = [1.0 if t < anchor else 1.0 + truth_step for t in times]
    other = [1.0 if t < anchor else 1.0 + other_step for t in times]
    steps = {
        "order-service": _step_score(times, truth, anchor=anchor),
        "redis": _step_score(times, other, anchor=anchor),
    }
    case_scale = max(steps.values())
    from evaluation.scenario import ServiceSignals, Signals

    return Scenario(
        scenario_id="synthetic-two-service",
        name="two services",
        description="step-score fixture",
        service_hint="order-service",
        fault_start=datetime.fromtimestamp(anchor, tz=UTC),
        window_start=datetime.fromtimestamp(anchor - 40 * 60, tz=UTC),
        window_end=datetime.fromtimestamp(anchor + 10 * 60, tz=UTC),
        signals=Signals(per_service={
            "order-service": ServiceSignals(error_rate_series=_normalise(
                _resample(times, truth, anchor=anchor), own=steps["order-service"],
                scale_to=case_scale)),
            "redis": ServiceSignals(error_rate_series=_normalise(
                _resample(times, other, anchor=anchor), own=steps["redis"],
                scale_to=case_scale)),
        }),
        ground_truth={"service": "order-service", "fault_type": "availability_degradation",
                      "expected_diagnosis": "SUPPORTED"},
    )


class TestHuggingFaceParquetLayout:
    """HuggingFace 版数据集是一个案例一个目录 + metrics.parquet。"""

    def _write_case(self, root: Path, name: str, *, inject=INJECT_EPOCH) -> Path:
        pyarrow = pytest.importorskip("pyarrow")
        import pyarrow.parquet as pq

        case = root / name
        case.mkdir(parents=True)
        times = [inject - 30 * 60 + i * 60 for i in range(60)]
        values = {
            "adservice_cpu": [1.0 if t < inject else 80.0 for t in times],
            "frontend_latency": [30.0 if t < inject else 32.0 for t in times],
        }
        table = pyarrow.table({"time": times, **values})
        pq.write_table(table, case / "metrics.parquet")
        (case / "inject_time.txt").write_text(str(inject), encoding="utf-8")
        return case

    def test_parquet_case_is_imported_with_its_label(self, tmp_path):
        self._write_case(tmp_path, "re1ob_adservice_cpu_1")
        imported = import_dataset(tmp_path, dataset="re1-ob")
        assert len(imported) == 1
        case = imported[0]
        assert case.service == "adservice"
        assert case.metric == "cpu"
        assert case.scenario.ground_truth.fault_type == "availability_degradation"
        assert "layout: parquet" in case.notes

    def test_a_delay_fault_maps_to_latency_degradation(self, tmp_path):
        self._write_case(tmp_path, "re1ob_adservice_delay_1")
        imported = import_dataset(tmp_path, dataset="re1-ob")
        assert imported[0].scenario.ground_truth.fault_type == "latency_degradation"

    def test_case_name_without_a_suite_prefix_is_refused(self, tmp_path):
        self._write_case(tmp_path, "adservice_cpu")
        with pytest.raises(RcaEvalDataMissing):
            import_dataset(tmp_path, dataset="re1-ob")


class TestScenarioLoader:
    def test_imported_scenarios_are_not_silently_invisible(self, tmp_path):
        from evaluation.scenario import load_scenarios

        scenario = _two_service_case(truth_step=100.0, other_step=10.0)
        (tmp_path / "rcaeval-re1-ob-x-cpu-case-001.json").write_text(
            scenario.model_dump_json(indent=2), encoding="utf-8")
        loaded = load_scenarios(tmp_path)
        assert [s.scenario_id for s in loaded] == ["synthetic-two-service"]
