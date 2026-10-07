"""模块级公开符号不允许"定义了却从未被引用"。

## 为什么需要这条

这个仓库反复出现的失败模式是**"声称完成但实际不成立"**：

- commit 33b4ce2 声称"核心运行时加上了结构化日志"，实际全仓只有 1 处
  `log.<level>` 调用 —— graph.py 和 executor.py 定义了 logger 却从不调用；
- `evidence/normalizer.py` 里的 `TimeRange` 类从未被引用（窗口时间实际用元组传）；
- `runtime/shutdown.py` 的 `install_stop_signal` 是 `StopSignal()` 的等价别名。

这些都不会让任何测试变红，因为它们**不破坏行为**。它们只是让代码库
逐渐积累"看起来有能力、实际没接线"的部分，读者会据此误判系统能力。

一条静态检查能在测试阶段就把它们暴露出来。

## 判定口径

- 只看**模块级**的公开函数/类/大写常量（下划线开头的视为私有，不算）
- 引用范围覆盖：agent-runtime/traceops、agent-runtime/tests、evaluation、scripts
- 排除定义行本身（否则每个符号都至少"被引用"一次）
- 用 AST 而不是正则：正则会把注释、字符串里的同名词也算成引用，
  造成假阴性（看起来被用了，其实没有）
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO / "agent-runtime" / "traceops"
SEARCH_ROOTS = [
    PACKAGE_ROOT,
    REPO / "agent-runtime" / "tests",
    REPO / "evaluation",
    REPO / "agent-runtime" / "scripts",
]


def _module_level_symbols(path: pathlib.Path) -> list[tuple[str, int]]:
    """返回 (名字, 行号) 列表，只含模块级公开符号。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                out.append((node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                out.append((node.name, node.lineno))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    out.append((target.id, node.lineno))
    return out


def _reference_counts(definitions: dict[str, list[tuple[pathlib.Path, int]]]) -> dict[str, int]:
    """统计每个名字在**定义行之外**被引用的次数。

    两个必须做对的细节：

    1. **一行可能引用多个名字**，必须全部计数。早先的写法在第一个匹配后
       就 `break`，于是 `from ... import Evidence, EvidenceRelation,
       EvidenceRelationType` 这一行只给 `Evidence` 记了数，
       EvidenceRelation 永远显示"未被引用" —— 假阴性。
    2. **必须按词边界匹配**。用 `name in line` 会让 `Evidence` 命中
       `EvidenceRelation` 内部的子串，产生同样的假阴性。
    """
    import re

    counts = {name: 0 for name in definitions}
    patterns = {
        name: re.compile(r"\b" + re.escape(name) + r"\b") for name in definitions
    }
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for lineno, line in enumerate(lines, 1):
                for name, pattern in patterns.items():
                    if not pattern.search(line):
                        continue
                    if any(path == f and lineno == ln for f, ln in definitions[name]):
                        continue
                    counts[name] += 1
    return counts


def test_no_module_level_symbol_is_never_referenced():
    definitions: dict[str, list[tuple[pathlib.Path, int]]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for name, lineno in _module_level_symbols(path):
            definitions.setdefault(name, []).append((path, lineno))

    assert definitions, "no module-level symbols found - the scanner is broken"

    counts = _reference_counts(definitions)

    dead = []
    for name, places in definitions.items():
        if counts.get(name, 0) == 0:
            for path, lineno in places:
                rel = path.relative_to(REPO)
                dead.append(f"{name} ({rel}:{lineno})")

    assert not dead, (
        "module-level public symbols that are never referenced anywhere in the "
        "repository - each one is either dead code or a capability that was "
        "wired up in name only:\n  " + "\n  ".join(sorted(dead)))
