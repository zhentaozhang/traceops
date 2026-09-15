"""RCAEval 适配器 CLI：`python -m evaluation.adapters.cli --dataset-dir <path>`。

用途：把本地已下载的 RCAEval 数据转成 TraceOps 观测，并跑一次真实调查，
输出与 synthetic 场景**同一形状**的指标。

为什么不直接接进 runner：RCAEval 的数据集与 TraceOps 场景是**两种来源**，
`评测口径 §1` 要求它们分列三档（synthetic / RCAEval / 靶场），
数字上必须可区分。合并进同一个 runner 会让"这些分数来自哪里"变得模糊。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from evaluation.adapters.rcaeval_adapter import (
    EvidenceOnlyAdapters, RcaEvalDataMissing, discover_cases, load_case,
)
from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    InvestigationRequest, RunContext, run_investigation,
)
from traceops.runtime.budget import BudgetManager


def _run_one(case_dir: Path, *, dataset: str) -> dict:
    result = load_case(case_dir, dataset=dataset)
    if not result.evidence:
        return {"case": str(case_dir), "status": "NO_EVIDENCE",
                "service": result.service, "metric": result.metric}

    store = EvidenceStore()
    store.register_source("rcaeval")
    for item in result.evidence:
        store.add(item)

    window_start = result.evidence[0].window_start
    window_end = result.evidence[0].window_end
    ctx = RunContext(store=store, adapters=EvidenceOnlyAdapters(result.evidence),
                     budget=BudgetManager(max_tool_calls=40), topology=None)
    investigation = run_investigation(
        InvestigationRequest(
            investigation_run_id=f"rcaeval-{result.case_id}",
            incident_id=f"rcaeval-{result.case_id}",
            environment="benchmark", service_hint=result.service,
            fault_start=window_end, window_start=window_start, window_end=window_end,
        ), ctx)
    report = investigation.report
    return {
        "case": result.case_id,
        "service": result.service,
        "metric": result.metric,
        "status": investigation.status,
        "diagnosis": report.diagnosis_status if report else None,
        "rank1_service": report.hypotheses[0].candidate_service
        if report and report.hypotheses else None,
        "evidence_cited": len(report.evidence) if report else 0,
        "errors": investigation.errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TraceOps × RCAEval adapter")
    parser.add_argument("--dataset-dir", required=True,
                        help="path to an RCAEval dataset tree (contains **/data.csv)")
    parser.add_argument("--dataset", required=True,
                        help="dataset id, e.g. re2-ob (recorded in output for provenance)")
    parser.add_argument("--limit", type=int, default=0, help="cap the number of cases")
    parser.add_argument("--out", type=Path, help="write JSON results here")
    args = parser.parse_args(argv)

    dataset_dir = Path(args.dataset_dir)
    try:
        cases = discover_cases(dataset_dir)
    except RcaEvalDataMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print(f"no RCAEval cases under {dataset_dir}", file=sys.stderr)
        return 2

    rows = [_run_one(case, dataset=args.dataset) for case in cases]
    hits = [r for r in rows if r.get("rank1_service") == r.get("service")]
    summary = {
        "dataset": args.dataset,
        "dataset_dir": str(dataset_dir),
        "cases": len(rows),
        "ac_at_1": round(len(hits) / len(rows), 4) if rows else 0.0,
        # 明确标注来源，避免与 synthetic 场景的分数混读
        "source": "rcaeval",
        "note": "external benchmark; do NOT compare directly with synthetic scenario metrics",
    }
    payload = {"summary": summary, "results": rows}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written {args.out}")
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
