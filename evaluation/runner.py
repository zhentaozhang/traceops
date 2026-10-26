"""评估 runner CLI。

用法（在 traceops/ 目录下）:
    python -m evaluation.runner                       # 跑 scenarios/ 全部场景
    python -m evaluation.runner --scenario F-001      # 只跑一个
    python -m evaluation.runner --out reports/latest.json

## 两个刻意的输出

1. `reports/latest.json`：**机器**读数（指标 + 机会基线）。
2. `reports/reports/<scenario>.md`：**人**读数（每个场景真实的诊断、证据、
   不确定的地方）。此前报告对象在 harness 里被丢掉，于是"这个场景诊断成了
   什么、凭什么"没有任何人能看到 —— 只有分数的评测无法被复核。

## 它会说明自己跑的是哪条路

`mode` 会写进 JSON：`rules-only` 或 `model:<名字>`。规则路径不需要模型，
但它的数字**不能**被当成"agent 的诊断能力" —— 说清楚这一点比多跑几个场景重要。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from evaluation.decomposition import baseline_comparison
from evaluation.harness import run_scenario
from evaluation.chance import compute_chance_metrics
from evaluation.metrics import aggregate
from evaluation.scenario import load_scenarios

DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
DEFAULT_OUT = Path(__file__).resolve().parent / "reports" / "latest.json"
DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent / "reports" / "reports"


def _build_model(args):
    """按参数/环境变量构造模型客户端；没配就返回 None（纯规则路径）。"""
    base_url = args.model_base_url or os.environ.get("TRACEOPS_LLM_BASE_URL")
    model_name = args.model_name or os.environ.get("TRACEOPS_LLM_MODEL")
    if not base_url or not model_name:
        return None
    from traceops.runtime.openai_client import OpenAICompatibleModel

    return OpenAICompatibleModel(
        base_url=base_url,
        model_name=model_name,
        api_key=args.model_api_key or os.environ.get("TRACEOPS_LLM_API_KEY"),
    )


def _write_human_reports(outcomes, directory: Path) -> int:
    """每个场景写一份可读诊断。返回写了几份。"""
    from traceops.rca.render import render_report

    directory.mkdir(parents=True, exist_ok=True)
    written = 0
    for outcome in outcomes:
        report = getattr(outcome, "report", None)
        if report is None:
            continue
        body = render_report(report, cost={"tool_calls": outcome.tool_calls})
        header = f"# {outcome.scenario_id}\n\n"
        footer = (
            "\n---\n\n"
            "这是合成场景的真实输出（数据是编的，编排是真的）。"
            "指标读 `reports/latest.json`；这份文件回答的是『凭什么』。\n"
        )
        (directory / f"{outcome.scenario_id}.md").write_text(header + body + footer,
                                                             encoding="utf-8")
        written += 1
    return written


def _format_row(outcome) -> str:
    diag = "ABSTAIN" if outcome.abstained else ("OK" if outcome.diagnosis_correct else "WRONG")
    return (
        f"{outcome.scenario_id:<8} diag={diag:<8} "
        f"AC@1={'Y' if outcome.service_ac_at_1 else 'n'} "
        f"FT@1={'Y' if outcome.fault_type_at_1 else 'n'} "
        f"recall={outcome.evidence_recall:.2f} "
        f"prec={outcome.citation_precision:.2f} "
        f"chain={'Y' if outcome.chain_order_correct else ('-' if outcome.chain_order_correct is None else 'n')} "
        f"aff={'Y' if outcome.affected_services_correct else ('-' if outcome.affected_services_correct is None else 'n')} "
        f"calls={outcome.tool_calls}"
    )


def _fmt_lift(value: float | None) -> str:
    """Lift 为 None 表示"没有可比基线"（候选池比 k 还小），不是"等于 1"。"""
    return "n/a" if value is None else f"{value:.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TraceOps synthetic scenario evaluation")
    parser.add_argument("--scenarios-dir", type=Path, default=DEFAULT_SCENARIOS_DIR)
    parser.add_argument("--scenario", action="append", default=None, help="scenario id, repeatable")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR,
                        help="每个场景写一份人类可读诊断到这里")
    parser.add_argument("--no-reports", action="store_true",
                        help="skip the human-readable per-scenario reports")
    parser.add_argument("--model-base-url", default=None,
                        help="OpenAI-compatible endpoint (env TRACEOPS_LLM_BASE_URL)")
    parser.add_argument("--model-name", default=None,
                        help="model name (env TRACEOPS_LLM_MODEL)")
    parser.add_argument("--model-api-key", default=None,
                        help="api key (env TRACEOPS_LLM_API_KEY)")
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios_dir)
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [s for s in scenarios if s.scenario_id in wanted]
    if not scenarios:
        print(f"no scenarios found in {args.scenarios_dir}")
        return 2

    model = _build_model(args)
    mode = f"model:{model.name}" if model is not None else "rules-only"
    print(f"mode={mode}")
    if model is None:
        print("     NOTE: 没有配置模型端点，跑的是纯规则路径。这些数字说明规则能做什么，"
              "不说明 agent 的诊断能力。", file=sys.stderr)

    outcomes = [run_scenario(s, model=model) for s in scenarios]
    agg = aggregate(outcomes)
    chance = compute_chance_metrics(outcomes)

    for outcome in outcomes:
        print(_format_row(outcome))
    print("-" * 72)
    print(
        f"n={agg.scenario_count} "
        f"AC@1={agg.ac_at_1:.2f} AC@3={agg.ac_at_3:.2f} AC@5={agg.ac_at_5:.2f} "
        f"FT@1={agg.fault_type_accuracy:.2f} "
        f"diag_acc={agg.diagnosis_accuracy:.2f} "
        f"abstain={agg.abstention_rate:.2f} "
        f"recall={agg.evidence_recall_mean:.2f} "
        f"prec={agg.citation_precision_mean:.2f} "
        f"chain={agg.chain_order_accuracy:.2f} "
        f"affected={agg.affected_services_accuracy:.2f}"
    )
    # 排名质量：AC@k 阶跃，Avg@5/MRR 连续 —— 一起看才不会被"进了前5"掩盖"排第5"
    print(
        f"     Avg@5={agg.avg_at_5:.2f} MRR={agg.mrr:.2f} mean_rank={agg.mean_rank:.2f} "
        f"wrong_ans={agg.wrong_answer_rate:.2f} must_id={agg.must_identify_rate:.2f}"
    )
    # 机会基线必须与 AC@k 一起打印，否则 AC@3/@5 会被当成能力
    print(
        f"     candidates={chance.candidate_pool_size} "
        f"chance@1={chance.chance_at_1:.2f} chance@3={chance.chance_at_3:.2f} "
        f"chance@5={chance.chance_at_5:.2f} "
        f"lift@1={_fmt_lift(chance.lift_at_1)} "
        f"lift@3={_fmt_lift(chance.lift_at_3)} lift@5={_fmt_lift(chance.lift_at_5)}"
    )
    # 漏斗：把"答错了"拆成"没看见 / 没进候选 / 排错了"三种，修法完全不同
    print(
        f"     漏斗：观测到 {agg.observed_rate:.2f} → 进入候选 {agg.candidate_coverage:.2f} "
        f"→ 排第一 {agg.ac_at_1:.2f}"
    )
    # 朴素基线：neither 看时间、拓扑，也不看日志 —— 我们必须赢过它们，
    # 否则"逐跳证据推理"这件事没有体现在结果上。
    print(
        f"     朴素基线 AC@1：峰值最高 {agg.peak_heuristic_at_1:.2f}"
        f"（表态 {agg.peak_heuristic_cases}/{len([o for o in outcomes if o.fault_injected])}）"
        f" ｜ 最早异常 {agg.earliest_heuristic_at_1:.2f}"
        f"（表态 {agg.earliest_heuristic_cases}/{len([o for o in outcomes if o.fault_injected])}）"
    )
    # 同分母对照：基线弃权的场景不算它输，我们也不能拿它撑分
    comparison = baseline_comparison(outcomes)
    for name, row in comparison.items():
        print(
            f"     同分母对照 {name}：我们 {row['ours_at_1']:.2f} vs 基线 "
            f"{row['theirs_at_1']:.2f}（{row['decided']} 个场景，基线弃权 {row['abstained']}）"
        )
    if chance.candidate_pool_size <= 5:
        print(
            f"     NOTE: candidate pool is only {chance.candidate_pool_size}; "
            "AC@3/AC@5 are near-tautological and Lift is undefined. "
            "Do not cite them as retrieval ability.",
            file=sys.stderr,
        )
    _ = (agg.wrong_answer_rate, agg.must_identify_rate)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "scenarios": [o.to_dict() for o in outcomes],
        "aggregate": agg.to_dict(),
        "chance_baseline": chance.to_dict(),
        "funnel": {
            "observed": agg.observed_rate,
            "in_candidates": agg.candidate_coverage,
            "top1": agg.ac_at_1,
        },
        "naive_baselines": baseline_comparison(outcomes),
        "note": "synthetic scenarios; not a real-world accuracy claim",
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {args.out}")

    if not args.no_reports:
        written = _write_human_reports(outcomes, args.reports_dir)
        print(f"written {written} human-readable reports to {args.reports_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
