"""盲测任务包的测试：导出的题不能泄答案，打分器的口径要与评测对得上。

这里的每一条都对着一个具体的失败模式：

* 题面里混进了真值 → 盲测变成开卷，**没有任何指标会发现**（所以有泄露断言）；
* 打分器与评测口径漂移 → 同一批报告在两处得到不同分数，外人无法判断信哪个；
* 弃权被记成答错 → 优势被虚高；
* 时间容差没生效 → "三元组"这个指标形同虚设。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.blind import (
    CASE_PREFIX,
    PackLeak,
    Prediction,
    assert_pack_is_blind,
    export_pack,
    load_answers,
    load_predictions,
    predictions_from_reports,
    score,
)
from evaluation.harness import run_scenario
from evaluation.metrics import aggregate
from evaluation.scenario import load_scenarios

SCENARIOS = load_scenarios("evaluation/scenarios")


@pytest.fixture(scope="module")
def pack(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("blind-pack")
    export_pack(SCENARIOS, root)
    return root


@pytest.fixture(scope="module")
def answers(pack: Path) -> dict[str, dict]:
    return load_answers(pack / "answers.json")


@pytest.fixture(scope="module")
def outcomes() -> list:
    return [run_scenario(scenario) for scenario in SCENARIOS]


def test_pack_has_one_task_per_case_and_a_separate_answer_key(pack: Path) -> None:
    case_dirs = sorted(path for path in (pack / "tasks").iterdir() if path.is_dir())
    assert len(case_dirs) == len(SCENARIOS)
    assert [path.name for path in case_dirs] == [
        f"{CASE_PREFIX}-{index:03d}" for index in range(1, len(SCENARIOS) + 1)
    ]
    assert (pack / "answers.json").exists()
    assert (pack / "FORMAT.md").exists()
    # 答案文件在 tasks/ **之外**：交给被测方的是 tasks/ 目录，不是整个包。
    assert not (pack / "tasks" / "answers.json").exists()


def test_task_json_carries_context_but_no_ground_truth(pack: Path) -> None:
    task = json.loads((pack / "tasks" / f"{CASE_PREFIX}-002" / "task.json").read_text(encoding="utf-8"))
    assert task["alert_summary"] is not None or task["service_hint"]
    assert "window_start" in task and "window_end" in task
    for forbidden in ("ground_truth", "fault_type", "must_identify", "wrong_answers"):
        assert forbidden not in task, f"task.json 泄露了 {forbidden}"

    answer = json.loads((pack / "answers.json").read_text(encoding="utf-8"))["answers"][1]
    assert answer["fault_type"] != "none", "第二个案例必须是有故障的，否则这条断言没意义"
    assert answer["scenario_id"] == SCENARIOS[1].scenario_id, "答案文件保留场景编号，只有出题方看得到"


def test_telemetry_payloads_are_the_raw_datasource_responses(pack: Path) -> None:
    """载荷必须是数据源原始形状：外部 agent 没有我们的归一化器。"""
    # case-006 是 F-005（三个服务），只有多服务场景才同时有三个数据源的载荷
    case = pack / "tasks" / f"{CASE_PREFIX}-006"
    assert (case / "task.json").exists(), "案例编号与场景顺序绑定，导出时不要重排"
    prometheus = json.loads((case / "telemetry" / "prometheus" / "redis.json").read_text(encoding="utf-8"))
    assert prometheus["data"]["resultType"] == "matrix"
    loki = json.loads((case / "telemetry" / "loki" / "redis.json").read_text(encoding="utf-8"))
    assert loki["data"]["resultType"] == "streams"
    tempo = json.loads((case / "telemetry" / "tempo" / "redis.json").read_text(encoding="utf-8"))
    assert "batches" in tempo


def test_empty_payloads_are_exported_not_omitted(pack: Path) -> None:
    """`result: []` 是"查了，没有"；不给文件等于"没查"，两件事不能混。"""
    prometheus = json.loads(
        (pack / "tasks" / f"{CASE_PREFIX}-001" / "telemetry" / "prometheus" / "order-service.json").read_text(
            encoding="utf-8"
        )
    )
    assert prometheus["data"]["result"] == []


def test_a_leaked_ground_truth_file_is_caught(tmp_path: Path) -> None:
    """导出后的自我保护：多写一个文件就能让盲测失效，所以要有断言。"""
    (tmp_path / "case-001").mkdir()
    (tmp_path / "case-001" / "task.json").write_text(
        json.dumps({"case_id": "case-001", "ground_truth": {"service": "redis"}}), encoding="utf-8"
    )
    with pytest.raises(PackLeak) as error:
        assert_pack_is_blind(tmp_path)
    assert "ground_truth" in str(error.value)


def test_export_refuses_to_clobber_a_non_empty_pack(pack: Path) -> None:
    with pytest.raises(FileExistsError):
        export_pack(SCENARIOS, pack)


def test_score_reproduces_the_evaluation_number(answers: dict[str, dict], outcomes: list) -> None:
    """同一次运行，盲测打分器与评测 runner 必须得到同一个 AC@1。

    对不上的话，外人看到的两个数字就会互相矛盾 —— 而这件事比"分数低"严重得多：
    它意味着我们自己的报告不可复现。
    """
    case_ids = [f"{CASE_PREFIX}-{index:03d}" for index in range(1, len(SCENARIOS) + 1)]
    predictions = predictions_from_reports(case_ids, [outcome.report for outcome in outcomes])
    result = score(predictions, answers)
    aggregate_outcome = aggregate(outcomes)

    blind_rate = result["aggregate"]["ac_at_1"]["rate"]
    assert blind_rate == pytest.approx(aggregate_outcome.ac_at_1, abs=1e-4), (
        f"盲测 {blind_rate} != 评测 {aggregate_outcome.ac_at_1}"
    )
    # 时间这一维在这批合成场景上是满分（序列最后一点就落在 fault_start 上），
    # 写死它是为了让"报告 → 预测"的 onset 映射出错时立刻红。
    assert result["aggregate"]["onset_at_1"]["rate"] == 1.0
    assert result["aggregate"]["triple_at_1"]["hits"] == result["aggregate"]["ac_at_1"]["hits"]


def test_the_one_miss_is_the_ranking_case(answers: dict[str, dict], outcomes: list) -> None:
    """F-010 是排序 miss：它必须表现为"服务猜错"，而不是"没有证据/没有候选"。"""
    case_ids = [f"{CASE_PREFIX}-{index:03d}" for index in range(1, len(SCENARIOS) + 1)]
    predictions = predictions_from_reports(case_ids, [outcome.report for outcome in outcomes])
    result = score(predictions, answers)
    misses = [row["case_id"] for row in result["cases"] if row.get("ac_at_1") is False]
    assert len(misses) == 1, misses
    missed = result["cases"][[row["case_id"] for row in result["cases"]].index(misses[0])]
    assert missed["ac_at_3"] is True, "真因仍在候选里 —— 所以这是排序 miss，不是检索 miss"


def test_a_wrong_answer_scores_zero_and_abstaining_on_a_fault_scores_zero(answers: dict[str, dict]) -> None:
    wrong = {case_id: Prediction(case_id=case_id, ranked=[{"service": "no-such-service"}]) for case_id in answers}
    result = score(wrong, answers)
    assert result["aggregate"]["ac_at_1"]["rate"] == 0.0

    abstained = {case_id: Prediction(case_id=case_id, abstain=True) for case_id in answers}
    result = score(abstained, answers)
    assert result["aggregate"]["ac_at_1"]["rate"] == 0.0
    assert result["aggregate"]["abstention_rate"]["rate"] == 1.0


def test_abstaining_on_a_case_without_a_fault_is_correct(answers: dict[str, dict]) -> None:
    no_fault = [case_id for case_id, answer in answers.items() if answer["fault_type"] == "none"]
    assert no_fault, "这批场景里必须有至少一个无故障对照"
    predictions = {case_id: Prediction(case_id=case_id, abstain=(case_id in no_fault)) for case_id in answers}
    result = score(predictions, answers)
    assert result["aggregate"]["no_fault_abstained_correctly"]["rate"] == 1.0


def test_onset_outside_the_tolerance_keeps_the_service_but_loses_the_triple(answers: dict[str, dict]) -> None:
    """时间差得多：服务仍算对，三元组算错 —— 两个指标必须能分开。"""
    case_id, answer = next(
        (case_id, answer) for case_id, answer in answers.items() if answer["fault_type"] != "none"
    )
    far = "2001-01-01T00:00:00+00:00"
    predictions = {
        case_id_: Prediction(
            case_id=case_id_,
            ranked=[
                {
                    "service": answer["service"],
                    "component": answer["component"],
                    "fault_type": answer["fault_type"],
                    "onset_at": far,
                }
            ]
            + [{"service": "no-such-service"}],
        )
        for case_id_, answer in answers.items()
        if answer["fault_type"] != "none"
    }
    result = score(predictions, answers)
    row = next(item for item in result["cases"] if item["case_id"] == case_id)
    assert row["ac_at_1"] is True
    assert row["onset_at_1"] is False
    assert row["triple_at_1"] is False


def test_missing_submissions_are_counted_not_skipped(answers: dict[str, dict]) -> None:
    """没提交的案例按弃权记，并且必须列出来 —— 静默跳过会让分母变小。"""
    result = score({}, answers)
    assert result["aggregate"]["missing_submissions"] == sorted(answers)
    assert result["aggregate"]["ac_at_1"]["denominator"] == sum(
        1 for answer in answers.values() if answer["fault_type"] != "none"
    )


def test_predictions_loader_accepts_a_bare_list_and_rejects_garbage(tmp_path: Path) -> None:
    path = tmp_path / "preds.json"
    path.write_text(
        json.dumps({"case-001": [{"service": "redis"}, {"service": "order-service"}]}), encoding="utf-8"
    )
    predictions = load_predictions(path)
    assert predictions["case-001"].ranked[0]["service"] == "redis"
    assert predictions["case-001"].abstain is False

    path.write_text(json.dumps({"case-001": "redis"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_predictions(path)
