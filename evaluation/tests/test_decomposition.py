"""把"答错了"拆成三步、并用两条朴素基线对照 —— 这几条数字必须能自我解释。

## 为什么必须写这些断言

分数本身无法指导修改。F-010 的 AC@1 是 n，但如果它"没看见真因服务"，
要改的是采集与约定；"看见了但没进候选"要改检测器；"进了候选但排第二"
要改排序。三种修法互不相干，所以 `funnel` 的三步必须**分别**被断言。

朴素基线同理：随机基线（0.25）太容易赢。真正的问题是"一眼能写出来的
启发式（谁峰值最高 / 谁最早异常）是不是已经能做到 0.9"。如果是，那
"逐跳证据推理"就没有体现在结果里。所以这里断言的不是"我们赢多少"，
而是**两条基线各自会输在哪**，这样未来变差时能立刻看出是哪条被追上了。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from evaluation.decomposition import (
    baseline_comparison, earliest_pick, funnel, heuristic_picks, peak_pick,
)
from evaluation.harness import run_scenario
from evaluation.metrics import aggregate
from evaluation.scenario import load_scenarios

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"


@pytest.fixture(scope="module")
def outcomes():
    return {s.scenario_id: (s, run_scenario(s)) for s in load_scenarios(SCENARIOS_DIR)}


def test_a_rank_two_miss_is_not_a_retrieval_miss(outcomes) -> None:
    """F-010：真因服务被观测到、也进了候选，只排在第二。

    这条断言是这次拆分的首要目的 —— 若把 F-010 笼统记成"诊断错误"，
    下一个人会去改采集或检测器，而真正要改的是排序。
    """
    scenario, outcome = outcomes["F-010"]
    link = funnel(outcome.report, scenario.ground_truth.service)
    assert link.observed is True, "真因服务的证据没被采集到，问题比排序更靠前"
    assert link.in_candidates is True, "真因服务没进候选，问题在检测器而不是排序"
    assert link.rank == 2
    assert link.top1 is False
    assert outcome.ground_truth_rank == link.rank


def test_every_injected_fault_was_actually_observed(outcomes) -> None:
    """观测率是这套系统最硬的一条：看见了才有资格排序。"""
    missing = [
        sid for sid, (scenario, outcome) in outcomes.items()
        if scenario.ground_truth.fault_type != "none"
        and not funnel(outcome.report, scenario.ground_truth.service).observed
    ]
    assert missing == [], f"这些场景里真因服务根本没被观测到：{missing}"


def test_the_healthy_control_has_no_funnel(outcomes) -> None:
    """没有注入故障的场景不存在"真因"，漏斗必须留空而不是 False。"""
    _, outcome = outcomes["F-000"]
    assert outcome.truth_observed is None
    assert outcome.truth_in_candidates is None
    assert outcome.heuristic_peak_top1 is None
    assert outcome.heuristic_earliest_top1 is None
    assert outcome.to_dict()["truth_observed"] is None


def test_peak_baseline_refuses_when_there_is_no_metric_data(outcomes) -> None:
    """F-002 是日志场景：没有指标就不该猜，返回 None 而不是"随便挑一个"。"""
    _, outcome = outcomes["F-002"]
    assert peak_pick(outcome.report) is None


def test_peak_baseline_blames_the_loudest_metric(outcomes) -> None:
    """F-004：真因服务就是指标最高的那个 —— 这条基线在这里会答对。"""
    scenario, outcome = outcomes["F-004"]
    assert peak_pick(outcome.report) == scenario.ground_truth.service


def test_earliest_baseline_blames_the_first_anomaly(outcomes) -> None:
    """F-003：只有 redis 先出现异常，这条基线应当答对。"""
    scenario, outcome = outcomes["F-003"]
    assert earliest_pick(outcome.report) == scenario.ground_truth.service


def test_earliest_baseline_abstains_when_two_services_tie(outcomes) -> None:
    """F-001：指标/日志/span 落在同一分钟，基线分辨不出来 → 弃权。

    这里刻意不让它靠字母序硬挑一个：硬挑出来的数字既不可复现，
    也无法解释（我们没法说"基线 0.36"到底代表什么能力）。
    """
    _, outcome = outcomes["F-001"]
    assert earliest_pick(outcome.report) is None
    peak, _ = heuristic_picks(outcome.report)
    assert peak == "order-service", "峰值基线会指向症状服务，这正是它输掉的地方"
    assert outcome.heuristic_earliest_top1 is None, "弃权必须留空，不能记成答错"


def test_the_ranking_beats_peak_but_only_ties_earliest(outcomes) -> None:
    """真实结论，不允许写成"我们赢了两条基线"。

    实测：峰值基线在自己表态的 9 个场景上是 0.44，我们是 0.89 —— 赢。
    最早异常基线在自己表态的 4 个场景上是 1.00，我们也是 1.00 —— **平手**。

    也就是说，我们的优势不体现在"能决定的场景上更准"，而体现在
    "剩下的 7 个场景它弃权、我们仍然答对 6 个"。把这条写死成断言，
    是为了防止下一个人（或下个月的我）在 README 里写成"超过两条基线"。
    """
    agg = aggregate(outcome for _, outcome in outcomes.values())
    assert agg.ac_at_1 > agg.peak_heuristic_at_1
    assert agg.ac_at_1 < agg.earliest_heuristic_at_1, (
        "总 AC@1 低于最早异常基线的 4 场景满分，这是预期：分母不同"
    )

    comparison = baseline_comparison([o for _, o in outcomes.values()])
    assert comparison["earliest_anomaly"]["ours_at_1"] == comparison["earliest_anomaly"]["theirs_at_1"]
    assert comparison["earliest_anomaly"]["abstained"] == 7


def test_the_comparison_uses_the_baselines_own_subset(outcomes) -> None:
    """同分母对照：基线弃权的场景不算它输，也不算我们赢。

    "最早异常"这条基线只在 4 个场景上表态。如果按 11 个场景的分母去比，
    我们的优势会被夸大 —— 优势其实全部来自"它弃权的 7 个场景"。
    """
    comparison = baseline_comparison([o for _, o in outcomes.values()])
    peak = comparison["peak_metric"]
    earliest = comparison["earliest_anomaly"]
    assert peak["decided"] + peak["abstained"] == 11
    assert earliest["decided"] == 4, earliest
    assert earliest["abstained"] == 7
    assert peak["ours_at_1"] > peak["theirs_at_1"], "在峰值基线愿意表态的场景上我们也必须更强"
    assert earliest["ours_at_1"] >= earliest["theirs_at_1"]


def test_aggregate_reports_both_funnel_rates(outcomes) -> None:
    agg = aggregate(outcome for _, outcome in outcomes.values())
    payload = agg.to_dict()
    for key in ("observed_rate", "candidate_coverage",
                "peak_heuristic_at_1", "earliest_heuristic_at_1"):
        assert key in payload
    assert 0.0 <= agg.observed_rate <= 1.0
    assert 0.0 <= agg.candidate_coverage <= 1.0


def test_the_runner_payload_carries_the_funnel(tmp_path, monkeypatch) -> None:
    """JSON 里必须出现漏斗与基线块，否则 CI 之外的人看不到这层拆分。"""
    import json

    from evaluation import runner

    out = tmp_path / "latest.json"
    code = runner.main([
        "--scenarios-dir", str(SCENARIOS_DIR),
        "--scenario", "F-004",
        "--out", str(out),
        "--no-reports",
    ])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["funnel"]["observed"] == 1.0
    assert payload["funnel"]["in_candidates"] == 1.0
    assert "peak_metric" in payload["naive_baselines"]
    assert "earliest_anomaly" in payload["naive_baselines"]
