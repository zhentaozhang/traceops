"""事故记忆的运维入口：看、以及把人的确认喂回去。

## 为什么需要它

控制面记录"这条结论被谁接受/拒绝了"（`POST /api/v1/incidents/{id}/confirm`），
但记忆库在调查面。两边之间**还没有**自动桥接的消费者 —— 所以现在的做法是
显式的：把控制面导出的确认记录交给这个命令应用。

为什么不假装它已经自动：一条"看起来会自动但实际不会"的链路，比一条
明确要人跑一次的命令更容易造成"记忆里应该有但事实上没有"的错觉。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from traceops.runtime.incident_memory import IncidentMemory
from traceops.runtime.paths import default_db_path


def _memory(args) -> IncidentMemory:
    return IncidentMemory(path=Path(args.memory_db) if args.memory_db else default_db_path("memory.db"))


def cmd_show(args) -> int:
    memory = _memory(args)
    try:
        rows = memory.summary(limit=args.limit)
    finally:
        memory.close()
    if not rows:
        print("memory: no priors recorded")
        return 0
    for row in rows:
        print(f"{row['service']:<24} {row['fault_type']:<26} "
              f"component={row['component'] or '-':<26} "
              f"hits={row['hits']} refuted={row['refuted']} confirmed={row['confirmed']}")
    return 0


def _confirmation(row: dict) -> tuple[str, str, str | None, bool] | None:
    """从一条确认记录里取出 (service, fault_type, component, accepted)。

    形状不对就跳过并警告，而不是让整批失败：一批确认里坏了一条，
    其余几十条仍然是有效的人工作业成果。
    """
    service = row.get("service")
    fault_type = row.get("fault_type")
    accepted = row.get("accepted")
    if not service or not fault_type or not isinstance(accepted, bool):
        return None
    component = row.get("component") or None
    return service, fault_type, component, accepted


def cmd_confirm(args) -> int:
    path = Path(args.json)
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"not valid JSON: {exc}", file=sys.stderr)
        return 2
    rows = payload.get("confirmations") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        print("expected a list, or an object with a 'confirmations' list", file=sys.stderr)
        return 2

    memory = _memory(args)
    applied = skipped = 0
    try:
        for row in rows:
            if not isinstance(row, dict):
                skipped += 1
                print(f"warning: skipping non-object entry: {row!r}", file=sys.stderr)
                continue
            parsed = _confirmation(row)
            if parsed is None:
                skipped += 1
                print(f"warning: skipping entry without service/fault_type/accepted: {row!r}",
                      file=sys.stderr)
                continue
            service, fault_type, component, accepted = parsed
            memory.record_confirmation(service=service, fault_type=fault_type,
                                       component=component, accepted=accepted)
            applied += 1
    finally:
        memory.close()
    print(json.dumps({"applied": applied, "skipped": skipped}))
    return 0


def _hoist(argv: list[str]) -> tuple[list[str], str | None]:
    """把 `--memory-db` 从任意位置提出来。

    argparse 的子命令只接受**它之后**的参数，所以 `memory show --memory-db x`
    会被判成"无法识别的参数"。而"路径写在动作后面"是人最自然的写法 ——
    与其让人背规则，不如接受两种位置。
    """
    remaining: list[str] = []
    value: str | None = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--memory-db" and index + 1 < len(argv):
            value = argv[index + 1]
            index += 2
            continue
        if item.startswith("--memory-db="):
            value = item.split("=", 1)[1]
            index += 1
            continue
        remaining.append(item)
        index += 1
    return remaining, value


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    rest, hoisted_db = _hoist(raw)
    parser = argparse.ArgumentParser(prog="traceops.cli.memory",
                                     description="inspect cross-incident memory, or apply "
                                                 "human confirmations to it")
    parser.add_argument("--memory-db", default=None,
                        help="SQLite path; defaults to $TRACEOPS_STATE_DIR/memory.db")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="print the strongest priors")
    show.add_argument("--limit", type=int, default=20)
    show.set_defaults(func=cmd_show)

    confirm = sub.add_parser("confirm", help="apply a confirmation export to memory")
    confirm.add_argument("--json", required=True,
                         help="JSON file: a list of {service, fault_type, component?, accepted}")
    confirm.set_defaults(func=cmd_confirm)

    args = parser.parse_args(rest)
    if hoisted_db is not None:
        args.memory_db = hoisted_db
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
