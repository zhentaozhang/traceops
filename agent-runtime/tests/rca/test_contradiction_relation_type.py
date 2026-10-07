"""矛盾模块应当是 CONTRADICTS 关系类型的**唯一**真值来源。

## 缺陷

`rca/contradiction.py` 提供了 `relation_type_of()`，它的文档定位是
"关系类型由矛盾模块自己定义"。但 `orchestration/graph.py:701` **硬编码**
了同一个常量：

    relation_type=RelationType.CONTRADICTS,

两处独立写同一个值 —— 将来若矛盾模块引入第二种关系类型（比如
REFUTES 与 CONTRADICTS 语义分离），graph 那一处会静默保持旧值，
产出"声称是矛盾、实际按反驳计分"的链接。

这类重复真值来源正是本仓库反复出现的"声称完成但实际不成立"的温床
（见 config snapshot、executor backend、logging logger）。

## 约束

graph 在构造 CONTRADICTS 链接时必须走矛盾模块的转换函数，
而不是自己写常量。
"""
from __future__ import annotations

import ast
import pathlib

from traceops.rca.contradiction import Contradiction, relation_type_of


def test_relation_type_of_returns_contradicts():
    from traceops.domain.models import RelationType

    c = Contradiction(hypothesis_id="h1", evidence_id="e1", reason="r")
    assert relation_type_of(c) == RelationType.CONTRADICTS


def test_graph_does_not_hardcode_the_contradicts_relation():
    """graph 里不应再出现裸的 RelationType.CONTRADICTS 字面量。"""
    import traceops.orchestration.graph as graph_module

    source = pathlib.Path(graph_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "CONTRADICTS":
            offenders.append(node.lineno)

    assert not offenders, (
        f"graph.py hardcodes RelationType.CONTRADICTS at lines {offenders}; "
        "it must use relation_type_of() from the contradiction module so the "
        "relation type has a single source of truth")
