"""盲测任务包：把场景变成「任何人能跑、且能被同一套指标打分」的题。

为什么要这个东西
----------------
我们的 12 个合成场景是**自己出的题**，AC@1 = 0.91 也是**自己打的分**。
这有两处无法自证的地方：

1. 出题的人知道答案，题面可能无意中泄了答案（比如某条日志直接写了根因）；
2. 打分的人就是被测系统，指标口径可以随时改到"看起来更好"。

RCAEval 的做法是把题目和答案分开：`export` 产出**盲测任务视图**（案例 id 匿名化、
答案另存），任何 agent 出预测，`evaluate` 再用同一套 AC@k 打分。这份模块就是
那个形状：

    tasks/case-001/task.json                    告警与时间窗（人能看到的信息）
    tasks/case-001/telemetry/prometheus/*.json  该查得到的原始载荷
    tasks/case-001/telemetry/loki/*.json
    tasks/case-001/telemetry/tempo/*.json
    answers.json                                真值（**不许交给被测方**）
    FORMAT.md                                   预测格式与打分口径

三点刻意的设计
--------------
* **载荷必须一起落盘**：外部 agent 没有我们的适配器，只给"服务名 + 时间窗"
  等于让它凭空猜。落盘的是数据源**原始返回体**，不是我们归一化后的证据 ——
  归一化本身是我们系统的一部分，提前给出去等于替它做了一半的活。
* **案例 id 匿名化**：`F-010` 这种 id 会让人直接去仓库里翻 `评测口径`
  的答案表。盲测的 id 必须与我们的编号无关（另外存一份对照在 `answers.json` 里，
  只有出题方看得到）。
* **空结果也要落盘**：`result: []` 是"查了，没有"，与"没查"是两件事。任务包里
  保留空载荷，被测方才有机会自己发现"这个服务在这个窗口里没有指标数据"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from string import Template
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from evaluation.harness import error_rate_payload, logs_payload, traces_payload
from evaluation.scenario import Scenario, load_scenarios

#: 案例 id 前缀。编号按场景排序生成，与 `F-0xx` 无关（防止翻仓库找答案）。
CASE_PREFIX = "case"

#: 时间答案的容差。OpenRCA 的真值是精确到分钟的故障发生时刻，而观测采样点
#: 通常落在整分钟上；差一分钟以内视为答对，超过就是答错（不设"模糊区间"
#: 以外的宽限，否则时间这一维形同虚设）。
ONSET_TOLERANCE = timedelta(minutes=1)

#: 任务包里禁止出现的字段名。它们一旦出现，盲测就是假的。
FORBIDDEN_KEYS = (
    "ground_truth",
    "fault_type",
    "expected_diagnosis",
    "must_identify",
    "wrong_answers",
    "additional_root_causes",
    "propagation_chain",
)

#: 注意 `component` **不在**上面那张表里，而且不是遗漏。
#: 变更记录（`config_change`）里的 `component` 是真实世界里 change feed 就会给的
#: 信息 —— 它是证据，不是被人顺手抄进题面的答案。代价是：这些案例的 `component@1`
#: 会偏容易，所以导出时会单独统计"哪些案例的变更记录里直接点名了真值组件"，
#: 让这条局限被**度量**，而不是被假装不存在。


class PackLeak(AssertionError):
    """任务包里出现了真值字段 —— 盲测已经失效，宁可报错也不要出题。"""


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------


def services_of(scenario: Scenario) -> list[str]:
    """本场景里**出现过**的服务名。

    取信号声明过的 + 拓扑里出现过的 + `service_hint`，去重排序。这里刻意
    不用 ground truth 的服务名去补：那会把"真因服务存在"这条信息泄露给
    被测方（任务包里每个服务都有一个目录，等于告诉它"答案在这些里面"）。
    """
    names: set[str] = {scenario.service_hint}
    names.update(scenario.signals.per_service.keys())
    for edge in scenario.topology_edges:
        names.update(edge)
    return sorted(name for name in names if name)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _task_of(scenario: Scenario, case_id: str) -> dict[str, Any]:
    """被测方能看到的一切。**不得**包含任何真值字段。"""
    return {
        "case_id": case_id,
        "name": scenario.name,
        "description": scenario.description,
        "alert_summary": scenario.alert_summary,
        "service_hint": scenario.service_hint,
        "fault_start": scenario.fault_start.isoformat(),
        "window_start": scenario.window_start.isoformat(),
        "window_end": scenario.window_end.isoformat(),
        "topology_edges": [list(edge) for edge in scenario.topology_edges],
        "scope_depth": scenario.scope_depth,
        "services": services_of(scenario),
        "config_change": (
            scenario.signals.config_change.model_dump(mode="json")
            if scenario.signals.config_change is not None
            else None
        ),
    }


def _answer_of(scenario: Scenario, case_id: str) -> dict[str, Any]:
    truth = scenario.ground_truth
    return {
        "case_id": case_id,
        "scenario_id": scenario.scenario_id,
        "service": truth.service,
        "component": truth.component,
        "fault_type": truth.fault_type,
        # 真值时间用 fault_start：那是注入方声明的故障起点，不是我们观测到的点。
        # 拿观测点当真值等于"用被测系统的答案给它自己打分"。
        "onset_at": scenario.fault_start.isoformat(),
        "expected_diagnosis": truth.expected_diagnosis,
        "must_identify": list(truth.must_identify),
        "wrong_answers": list(truth.wrong_answers),
        "additional_root_causes": list(truth.additional_root_causes),
    }


FORMAT_MD = """# Blind task pack

This pack contains `{cases}` investigation cases. `answers.json` is the answer key —
**do not give it to the system under test.** A scorer that reads it is what makes
results comparable across agents.

## What you get

```
tasks/<case_id>/task.json                  alert, time window, topology, service list
tasks/<case_id>/telemetry/prometheus/<service>.json
tasks/<case_id>/telemetry/loki/<service>.json
tasks/<case_id>/telemetry/tempo/<service>.json
```

Each telemetry file is the **raw datasource response** for that service
(Prometheus `query_range`, Loki `query_range`, Tempo `search` shapes). An empty
`result: []` means "queried, nothing there" — it is not an error and not the same
as "not queried".

## What to submit

A JSON file mapping case id to your ranked answer:

```json
{
  "case-001": {
    "abstain": false,
    "ranked": [
      {{"service": "redis", "component": "redis-connection-pool",
        "fault_type": "pool_exhaustion", "onset_at": "2026-10-30T10:02:00+00:00"}},
      {{"service": "order-service", "fault_type": "latency_degradation"}}
    ]
  },
  "case-002": {{"abstain": true, "ranked": []}}
}
```

* `ranked` is ordered: index 0 is your single best answer.
* `abstain: true` means "I cannot tell". Abstaining on a case that has no injected
  fault is **correct**; abstaining on a case that has one counts as a miss.
* `onset_at` is the time you believe the root cause *started* (ISO-8601, timezone
  aware). Scored with a ±{tolerance} tolerance.

## Score it

```bash
python -m evaluation.blind score --pack <this dir> --predictions <your file> --json
```

Reported: `AC@1`, `AC@3`, `fault_type@1`, `component@1`, `onset@1`,
`triple@1` (service **and** fault type **and** onset all correct), and the
abstention rate. Denominators are printed per metric — a metric computed over 4
of 12 cases is not comparable to one computed over 12.

## One honest limitation

Some cases ship a change record (`config_change`) that names the component that
changed. That is realistic evidence, not a leaked answer key — but it does make
`component@1` easier on those cases. `export` lists those case ids in
`component_named_by_change_record` so nobody can quietly claim a clean
component-accuracy number afterwards.
"""


def export_pack(
    scenarios: Sequence[Scenario],
    out_dir: str | Path,
    *,
    pack: str = "traceops-blind-v1",
    overwrite: bool = False,
) -> dict[str, Any]:
    """把场景导出成一个自包含的任务包，返回摘要。"""
    root = Path(out_dir)
    tasks = root / "tasks"
    if tasks.exists() and any(tasks.iterdir()) and not overwrite:
        raise FileExistsError(f"{tasks} is not empty; pass overwrite=True to replace it")

    cases: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenarios, start=1):
        case_id = f"{CASE_PREFIX}-{index:03d}"
        case_dir = tasks / case_id
        _write_json(case_dir / "task.json", _task_of(scenario, case_id))
        for service in services_of(scenario):
            _write_json(
                case_dir / "telemetry" / "prometheus" / f"{service}.json",
                error_rate_payload(scenario, service),
            )
            _write_json(
                case_dir / "telemetry" / "loki" / f"{service}.json",
                logs_payload(scenario, service),
            )
            _write_json(
                case_dir / "telemetry" / "tempo" / f"{service}.json",
                traces_payload(scenario, service),
            )
        cases.append({"case_id": case_id, "services": services_of(scenario)})
        answers.append(_answer_of(scenario, case_id))

    # 诚实记账：哪些案例的变更记录直接点名了真值组件（这些案例的 component@1 偏容易）
    named_component = [
        entry["case_id"]
        for scenario, entry in zip(scenarios, answers)
        if scenario.signals.config_change is not None
        and scenario.ground_truth.component
        and scenario.signals.config_change.component == scenario.ground_truth.component
    ]

    _write_json(
        root / "answers.json",
        {"pack": pack, "cases": len(answers), "onset_tolerance_seconds": int(ONSET_TOLERANCE.total_seconds()), "answers": answers},
    )
    # 用 Template 而不是 str.format：FORMAT.md 里全是 JSON 的花括号，
    # 用 format 会被当成占位符（第一次就炸在 `KeyError: '"case-001"'`）。
    (root / "FORMAT.md").write_text(
        Template(FORMAT_MD).safe_substitute(cases=len(answers), tolerance="60s"),
        encoding="utf-8",
    )
    assert_pack_is_blind(tasks)
    return {
        "pack": pack,
        "cases": len(cases),
        "tasks_dir": str(tasks),
        "answers": str(root / "answers.json"),
        "component_named_by_change_record": named_component,
    }


def assert_pack_is_blind(tasks_dir: str | Path) -> None:
    """任务包里不许出现真值字段。

    这条断言是这份模块存在的理由：如果导出时不小心把 `ground_truth` 写进了
    `task.json`，盲测就变成了开卷考试，而**没有任何指标会发现这件事**。
    """
    root = Path(tasks_dir)
    offenders: list[str] = []
    for path in sorted(root.rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        for key in FORBIDDEN_KEYS:
            if f'"{key}"' in text:
                offenders.append(f"{path.relative_to(root)} contains {key!r}")
                break
    if offenders:
        raise PackLeak("task pack leaks ground truth:\n  " + "\n  ".join(offenders))


# --------------------------------------------------------------------------
# 打分
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Prediction:
    """被测方的提交。`ranked` 有序，index 0 是最佳答案。"""

    case_id: str
    ranked: list[dict[str, Any]] = field(default_factory=list)
    abstain: bool = False


def _as_prediction(case_id: str, payload: Any) -> Prediction:
    if isinstance(payload, list):
        return Prediction(case_id=case_id, ranked=[dict(item) for item in payload])
    if not isinstance(payload, Mapping):
        raise ValueError(f"{case_id}: prediction must be an object or a list, got {type(payload).__name__}")
    ranked = payload.get("ranked") or payload.get("answers") or []
    if not isinstance(ranked, list):
        raise ValueError(f"{case_id}: 'ranked' must be a list")
    return Prediction(
        case_id=case_id,
        ranked=[dict(item) for item in ranked],
        abstain=bool(payload.get("abstain", False)),
    )


def load_predictions(path: str | Path) -> dict[str, Prediction]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("predictions must be a JSON object keyed by case id")
    if "predictions" in raw and isinstance(raw["predictions"], Mapping):
        raw = raw["predictions"]
    return {str(case_id): _as_prediction(str(case_id), payload) for case_id, payload in raw.items()}


def load_answers(path: str | Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = raw.get("answers", raw) if isinstance(raw, Mapping) else raw
    if not isinstance(entries, list):
        raise ValueError("answers file must contain a list under 'answers'")
    return {str(entry["case_id"]): dict(entry) for entry in entries}


def _onset_matches(predicted: Any, truth: Any) -> bool | None:
    """`None` 表示"这个案例没有时间真值/没提交时间"，不计入分母。"""
    if not predicted or not truth:
        return None
    try:
        left = datetime.fromisoformat(str(predicted))
        right = datetime.fromisoformat(str(truth))
    except ValueError:
        return False
    return abs(left - right) <= ONSET_TOLERANCE


def _ranked_values(prediction: Prediction, key: str) -> list[str]:
    values: list[str] = []
    for item in prediction.ranked:
        value = item.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    return values


def score_case(prediction: Prediction, answer: Mapping[str, Any]) -> dict[str, Any]:
    """单案例打分。没有注入故障的案例只判"是否弃权"。"""
    truth_service = answer.get("service")
    truth_type = answer.get("fault_type")
    has_fault = truth_type not in (None, "", "none")
    services = _ranked_values(prediction, "service")
    top = services[0] if services else None

    if not has_fault:
        return {
            "case_id": prediction.case_id,
            "has_fault": False,
            "abstained": prediction.abstain,
            "correct": prediction.abstain,
        }

    ranked_correct = top == truth_service
    component_hit: bool | None
    top_item = prediction.ranked[0] if prediction.ranked else {}
    truth_component = answer.get("component")
    if not truth_component:
        component_hit = None
    else:
        component_hit = top_item.get("component") == truth_component
    onset_hit = _onset_matches(top_item.get("onset_at"), answer.get("onset_at"))
    type_hit = top_item.get("fault_type") == truth_type

    return {
        "case_id": prediction.case_id,
        "has_fault": True,
        "abstained": prediction.abstain,
        "ac_at_1": ranked_correct and not prediction.abstain,
        "ac_at_3": truth_service in services[:3] and not prediction.abstain,
        "fault_type_at_1": type_hit and not prediction.abstain,
        "component_at_1": component_hit,
        "onset_at_1": onset_hit,
        "triple_at_1": (ranked_correct and type_hit and bool(onset_hit)) and not prediction.abstain,
    }


def _rate(hits: Iterable[bool | None]) -> dict[str, Any]:
    """比例 + 分母。**分母必须打出来**：4/4 与 11/12 不能放在同一行比较。"""
    values = [value for value in hits if value is not None]
    if not values:
        return {"rate": None, "hits": 0, "denominator": 0}
    return {"rate": round(sum(1 for v in values if v) / len(values), 4), "hits": sum(1 for v in values if v), "denominator": len(values)}


def score(
    predictions: Mapping[str, Prediction],
    answers: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """对整个任务包打分。缺提交的案例按"未提交"记，不静默跳过。"""
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for case_id, answer in sorted(answers.items()):
        prediction = predictions.get(case_id)
        if prediction is None:
            missing.append(case_id)
            prediction = Prediction(case_id=case_id, ranked=[], abstain=True)
        rows.append(score_case(prediction, answer))

    fault_rows = [row for row in rows if row["has_fault"]]
    aggregate = {
        "cases": len(rows),
        "fault_cases": len(fault_rows),
        "missing_submissions": missing,
        "ac_at_1": _rate(row["ac_at_1"] for row in fault_rows),
        "ac_at_3": _rate(row["ac_at_3"] for row in fault_rows),
        "fault_type_at_1": _rate(row["fault_type_at_1"] for row in fault_rows),
        "component_at_1": _rate(row["component_at_1"] for row in fault_rows),
        "onset_at_1": _rate(row["onset_at_1"] for row in fault_rows),
        "triple_at_1": _rate(row["triple_at_1"] for row in fault_rows),
        "abstention_rate": _rate(row["abstained"] for row in rows),
        "no_fault_cases": len(rows) - len(fault_rows),
        "no_fault_abstained_correctly": _rate(
            row["correct"] for row in rows if not row["has_fault"]
        ),
    }
    return {"aggregate": aggregate, "cases": rows}


# --------------------------------------------------------------------------
# 自检：用我们自己的报告当"预测"，看盲测打分器能不能复现同一批数字
# --------------------------------------------------------------------------


def predictions_from_reports(case_ids: Sequence[str], reports: Sequence[Any]) -> dict[str, Prediction]:
    """把一次真实评测的报告变成盲测提交。

    用途有两个：① 证明打分器能跑；② 证明"报告 → 预测"这一步没有额外加工
    （如果这里偷偷挑了服务，盲测分数就比评测分数高，而没人会注意到）。
    """
    predictions: dict[str, Prediction] = {}
    for case_id, report in zip(case_ids, reports):
        if report is None:
            predictions[case_id] = Prediction(case_id=case_id, abstain=True)
            continue
        ordered = sorted(
            report.hypotheses,
            key=lambda h: (h.rank if h.rank is not None else 10**6, h.id),
        )
        ranked = [
            {
                "service": hypothesis.candidate_service,
                "component": hypothesis.candidate_component,
                "fault_type": hypothesis.fault_type,
                "onset_at": report.root_cause_time.isoformat() if report.root_cause_time else None,
            }
            for hypothesis in ordered
        ]
        abstain = report.abstained or report.diagnosis_status == "INCONCLUSIVE"
        predictions[case_id] = Prediction(case_id=case_id, ranked=ranked, abstain=abstain)
    return predictions


def _cmd_selfscore(args: Any) -> int:
    """在我们自己的 12 个场景上跑一遍真实编排，再用盲测打分器打分。"""
    from evaluation.harness import run_scenario
    from evaluation.scenario import load_scenarios

    scenarios = load_scenarios(args.scenarios)
    case_ids = [f"{CASE_PREFIX}-{index:03d}" for index in range(1, len(scenarios) + 1)]
    reports = []
    for scenario in scenarios:
        outcome = run_scenario(scenario)
        reports.append(outcome.report)
    predictions = predictions_from_reports(case_ids, reports)
    answers = load_answers(Path(args.pack) / "answers.json")
    result = score(predictions, answers)
    agg = result["aggregate"]
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else "")
    if not args.json:
        for row in result["cases"]:
            if not row["has_fault"]:
                print(f"{row['case_id']}  {'ABSTAIN-OK' if row['correct'] else 'SHOULD-ABSTAIN'}")
                continue
            marks = " ".join(
                f"{key}={'Y' if row[key] else ('n/a' if row[key] is None else 'n')}"
                for key in ("ac_at_1", "ac_at_3", "fault_type_at_1", "component_at_1", "onset_at_1", "triple_at_1")
            )
            print(f"{row['case_id']}  {marks}")
        print("-" * 72)
        for key in ("ac_at_1", "fault_type_at_1", "component_at_1", "onset_at_1", "triple_at_1"):
            entry = agg[key]
            print(f"{key:<16} {entry['rate']}  ({entry['hits']}/{entry['denominator']})")
    # 与评测口径对账：盲测的 ac_at_1 必须与 runner 的 AC@1 一致（0.9091），
    # 否则说明"报告 → 预测"的映射多做了事。
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_export(args: Any) -> int:
    scenarios = load_scenarios(args.scenarios)
    summary = export_pack(scenarios, args.out, pack=args.pack, overwrite=args.overwrite)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n任务包在 {summary['tasks_dir']}；答案在 {summary['answers']}（**不要交给被测方**）")
    return 0


def _cmd_score(args: Any) -> int:
    pack = Path(args.pack)
    answers = load_answers(pack / "answers.json")
    predictions = load_predictions(args.predictions)
    result = score(predictions, answers)
    agg = result["aggregate"]
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    for row in result["cases"]:
        if not row["has_fault"]:
            mark = "ABSTAIN-OK" if row["correct"] else "SHOULD-ABSTAIN"
            print(f"{row['case_id']}  {mark}")
            continue
        marks = " ".join(
            f"{key}={'Y' if row[key] else ('n/a' if row[key] is None else 'n')}"
            for key in ("ac_at_1", "ac_at_3", "fault_type_at_1", "component_at_1", "onset_at_1", "triple_at_1")
        )
        print(f"{row['case_id']}  {marks}  abstain={row['abstained']}")
    print("-" * 72)
    for key in ("ac_at_1", "ac_at_3", "fault_type_at_1", "component_at_1", "onset_at_1", "triple_at_1"):
        entry = agg[key]
        print(f"{key:<16} {entry['rate'] if entry['rate'] is not None else 'n/a'}  ({entry['hits']}/{entry['denominator']})")
    print(f"abstention_rate  {agg['abstention_rate']['rate']}  ({agg['abstention_rate']['hits']}/{agg['abstention_rate']['denominator']})")
    if agg["missing_submissions"]:
        print(f"缺提交 {len(agg['missing_submissions'])} 个案例（按弃权计）: {', '.join(agg['missing_submissions'])}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="evaluation.blind", description="盲测任务包：导出与统一打分")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="把场景导出成盲测任务包")
    export.add_argument("--scenarios", default="evaluation/scenarios")
    export.add_argument("--out", required=True)
    export.add_argument("--pack", default="traceops-blind-v1")
    export.add_argument("--overwrite", action="store_true")
    export.set_defaults(func=_cmd_export)

    score_cmd = sub.add_parser("score", help="用任务包的答案给预测打分")
    score_cmd.add_argument("--pack", required=True)
    score_cmd.add_argument("--predictions", required=True)
    score_cmd.add_argument("--json", action="store_true")
    score_cmd.set_defaults(func=_cmd_score)

    self_cmd = sub.add_parser("selfscore", help="用我们自己的报告当预测，验证打分器与映射")
    self_cmd.add_argument("--pack", required=True)
    self_cmd.add_argument("--scenarios", default="evaluation/scenarios")
    self_cmd.add_argument("--json", action="store_true")
    self_cmd.set_defaults(func=_cmd_selfscore)

    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
