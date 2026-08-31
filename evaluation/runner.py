"""评估 runner CLI。

用法（在 traceops/ 目录下）:
    python -m evaluation.runner                       # 跑 scenarios/ 全部场景
    python -m evaluation.runner --scenario F-001      # 只跑一个
    python -m evaluation.runner --out reports/latest.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from evaluation.harness import run_scenario
from evaluation.chance import compute_chance_metrics
from evaluation.metrics import aggregate
from evaluation.scenario import load_scenarios

DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
DEFAULT_OUT = Path(__file__).resolve().parent / "reports" / "latest.json"


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
    args = parser.parse_args(argv)

    scenarios = load_scenarios(args.scenarios_dir)
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [s for s in scenarios if s.scenario_id in wanted]
    if not scenarios:
        print(f"no scenarios found in {args.scenarios_dir}")
        return 2

    outcomes = [run_scenario(s) for s in scenarios]
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
    # 机会基线必须与 AC@k 一起打印，否则 AC@3/@5 会被当成能力
    print(
        f"     candidates={chance.candidate_pool_size} "
        f"chance@1={chance.chance_at_1:.2f} chance@3={chance.chance_at_3:.2f} "
        f"chance@5={chance.chance_at_5:.2f} "
        f"lift@1={_fmt_lift(chance.lift_at_1)} "
        f"lift@3={_fmt_lift(chance.lift_at_3)} lift@5={_fmt_lift(chance.lift_at_5)}"
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
        "scenarios": [o.to_dict() for o in outcomes],
        "aggregate": agg.to_dict(),
        "chance_baseline": chance.to_dict(),
        "note": "synthetic scenarios; not a real-world accuracy claim",
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
