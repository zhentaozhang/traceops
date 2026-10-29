"""`python -m evaluation.importers.cli` —— 把 RCAEval 数据集转成场景文件。

两条命令：

* `import-rcaeval --dataset-dir <dir> --out <dir>`：转换并落盘，打印汇总；
* `probe --dataset-dir <dir>`：只看数据集形状（有几个案例、几个服务、
  候选池多大），不写文件 —— 用于"数据集下载对不对"的第一眼判断。

退出码：0 成功；2 数据集缺失或不可导入（**不返回空列表**：空列表会让
"还没下载"看起来像"这数据集没有异常"）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evaluation.adapters.rcaeval_adapter import RcaEvalDataMissing
from evaluation.importers.rcaeval import (
    import_dataset,
    summarise,
    write_scenarios,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evaluation.importers.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    convert = sub.add_parser("import-rcaeval", help="convert a RCAEval dataset into scenarios")
    convert.add_argument("--dataset-dir", required=True, type=Path)
    convert.add_argument("--out", required=True, type=Path,
                         help="directory to write <scenario_id>.json into")
    convert.add_argument("--name", default=None, help="dataset name (defaults to the directory name)")
    convert.add_argument("--limit", type=int, default=None, help="stop after N imported cases")

    probe = sub.add_parser("probe", help="report the dataset shape without writing anything")
    probe.add_argument("--dataset-dir", required=True, type=Path)
    probe.add_argument("--name", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        imported = import_dataset(args.dataset_dir, dataset=args.name, limit=getattr(args, "limit", None))
    except RcaEvalDataMissing as exc:
        print(json.dumps({"status": "MISSING_DATA", "reason": str(exc)}, ensure_ascii=False))
        return 2

    summary = summarise(imported)
    if args.command == "probe":
        print(json.dumps({"status": "OK", **summary}, ensure_ascii=False))
        return 0

    written = write_scenarios(imported, args.out)
    print(json.dumps(
        {"status": "OK", **summary, "written": len(written), "out": str(args.out)},
        ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
