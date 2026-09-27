"""E1–E5 消融的真实运行器。

用法：

    # 只跑确定性臂（E3/E4/E5，不需要模型）
    python -m evaluation.ablation_runner

    # 跑全部五臂（需要本机 Ollama）
    python -m evaluation.ablation_runner --ollama http://localhost:11434 --model qwen3:4b

## 为什么单独一个 runner 而不是塞进 evaluation.runner

`evaluation.runner` 的职责是"跑场景出指标"，它不该关心用哪个臂。
消融是**对比**，需要把同一批场景在多个配置下各跑一遍并算出差异 ——
混在一起会让两者都变复杂。

## 诚实边界

E1/E2 需要真实模型。没有 `--ollama` 时它们会被**显式标记为 skipped**，
而不是伪造一个结果。这与 `ablation.runnable_arms(has_model=False)` 的语义一致。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evaluation.ablation import ARMS, arm_profile, runnable_arms
from evaluation.harness import run_scenario
from evaluation.scenario import load_scenarios


def _ollama_model(base_url: str, model_name: str):
    from traceops.runtime.openai_client import OpenAICompatibleModel

    return OpenAICompatibleModel(
        base_url=base_url, model_name=model_name, ollama_native=True, timeout=120.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the E1-E5 ablation")
    parser.add_argument("--scenarios", default="evaluation/scenarios")
    parser.add_argument("--ollama", default=None,
                        help="Ollama base URL, e.g. http://localhost:11434")
    parser.add_argument("--model", default="qwen3:4b")
    parser.add_argument("--out", default="evaluation/reports/ablation.json")
    args = parser.parse_args(argv)

    scenarios = load_scenarios(Path(args.scenarios))
    model = _ollama_model(args.ollama, args.model) if args.ollama else None
    runnable = runnable_arms(has_model=model is not None)

    print(f"scenarios: {len(scenarios)}  model: {args.model if model else 'none'}")
    print(f"runnable arms: {sorted(runnable)}\n")

    results: dict[str, list] = {}
    for arm_id in ("E1", "E2", "E3", "E4", "E5"):
        profile = arm_profile(arm_id)
        if arm_id not in runnable:
            print(f"{arm_id}: SKIPPED (needs a model)")
            results[arm_id] = []
            continue
        outcomes = []
        for scenario in scenarios:
            try:
                outcome = run_scenario(scenario, model=model if profile.enable_model else None)
                outcomes.append({
                    "scenario_id": scenario.scenario_id,
                    "ac1": outcome.service_ac_at_1,
                    "fault1": outcome.fault_type_at_1,
                    "diagnosis_correct": outcome.diagnosis_correct,
                    "abstained": outcome.abstained,
                    "ranked": getattr(outcome, "ground_truth_rank", None),
                })
            except Exception as exc:  # 单个场景失败不终止整轮消融
                outcomes.append({"scenario_id": scenario.scenario_id,
                                 "error": str(exc)[:200]})
        results[arm_id] = outcomes
        ok = sum(1 for o in outcomes if o.get("ac1"))
        print(f"{arm_id}: {ok}/{len(scenarios)} AC@1  (profile={profile.digest()})")

    # 汇总对比：这是消融唯一有价值的输出
    print("\n--- 对比 ---")
    for arm_id in ("E1", "E2", "E3", "E4", "E5"):
        rows = results.get(arm_id) or []
        if not rows:
            print(f"{arm_id}: skipped")
            continue
        correct = sum(1 for r in rows if r.get("ac1"))
        errors = sum(1 for r in rows if r.get("error"))
        print(f"{arm_id}: AC@1={correct}/{len(rows)}"
              + (f"  errors={errors}" if errors else ""))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "model": args.model if model else None,
        "scenarios": [s.scenario_id for s in scenarios],
        "runnable_arms": sorted(runnable),
        "arms": {a: [{"digest": arm_profile(a).digest()}][0] for a in ARMS},
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
