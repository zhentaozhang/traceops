"""策略指纹与运行快照：把"同一个策略"和"同一次运行"分成两件事。

此前的做法是把两者塞进同一批字符串：

    workflow_version = f"p0-v1;max_tool_calls={max_tool_calls}"
    topology_version = f"file-declared;scope_depth={scope_depth}"

后果有两个，方向相反：

1. **同样的策略、不同的预算，看起来像不同的策略。** 预算从 50 改成 60，
   `workflow_version` 就变了，于是"这两次结论不同是不是因为换了策略"
   这个问题永远答成"是"，而实际上决策规则一个字都没动。
2. **真正换了策略，却看不出来。** 改 `ERROR_RATE_THRESHOLD`、改排序权重、
   改检测关键词、改提示词 —— 这些才是会改变结论的东西，
   但没有任何一个出现在快照里。

所以这里做一个干净的切分：

- `strategy_digest` = **决策策略的内容哈希**。同一套规则/阈值/权重/
  闸门/提示词 → 同一个 digest；改动其中任何一个 → 换 digest。
  它回答的是"这个结论是**哪套判断标准**下的结论"。
- `run_snapshot` = **本次运行的实际输入**。预算、采集深度、拓扑、
  提示词版本、告警原文是否存在。它回答的是"这次跑用掉了多少额度、
  看到了多少东西"。

两者都进报告，但语义不同：比策略要 digest 相等才可比，
比资源消耗要看 snapshot。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

#: 策略结构版本。**改变了 digest 的组成方式**（不是改变了策略本身）才动它 ——
#: 否则历史报告会因为一次重构而被判成"不同策略"。
STRATEGY_REVISION = "strategy-v1"

#: 工具 schema 版本。工具入参形状变了，模型看到的世界就变了。
TOOL_SCHEMA_VERSION = "p0-v1"


def _digest(payload: Any) -> str:
    """规范化 JSON 的内容哈希（16 位十六进制）。

    用 `sort_keys` + 紧凑分隔符：字典顺序不该影响指纹，
    否则 `dict` 的构造顺序会变成一次"策略变更"。
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def strategy_components(
    *,
    enable_semantic_gate: bool = True,
    enable_contradiction: bool = True,
    shadow_gates: frozenset[str] | set[str] | None = None,
    prompt_version: str | None = None,
    tool_schema_version: str = TOOL_SCHEMA_VERSION,
) -> dict[str, Any]:
    """把**会改变结论的旋钮**收集成一个可哈希的结构。

    这里刻意去 import 阈值与权重常量，而不是让调用方传进来：
    传进来的写法一定会漏（漏掉的那个旋钮就悄悄改了结论还说自己没改）。
    常量在哪定义，指纹就从哪读。
    """
    from traceops.config.fields import current_knobs

    # 旋钮的值**只从这里读**（`config/fields.py` 的声明）。
    # 以前这里手抄了 8 个常量名，于是"哪个旋钮参与指纹"有两处需要维护：
    # 新增一个阈值时忘了加进这个字典，指纹就不覆盖它 —— 换了策略却
    # 报"策略没变"。声明式字段把这两处合并成一处。
    knobs = current_knobs()

    return {
        "revision": STRATEGY_REVISION,
        "knobs": knobs,
        # 闸门开关决定"结论会不会被降级"，属于策略，不属于运行输入。
        # 影子化同样改变结论（只是方向相反），所以也在里面 —— 且带名字，
        # 因为"影子 ambiguity 闸门"和"影子 semantic 闸门"是两个不同策略。
        "gates": {
            "semantic": bool(enable_semantic_gate),
            "contradiction": bool(enable_contradiction),
            "shadow": sorted(shadow_gates or ()),
        },
        "prompt": prompt_version,
        "tool_schema": tool_schema_version,
    }


def strategy_digest(**kwargs: Any) -> str:
    """只需要一个字符串时用它；要解释差异用 `strategy_components`。"""
    return _digest(strategy_components(**kwargs))


def topology_digest(topology: Any | None) -> str | None:
    """拓扑的内容指纹。没有拓扑就是 `None`，不是空哈希。

    `None` 与 `"<空图>"` 必须能区分：前者是"没提供拓扑、退化为单服务"，
    后者是"提供了拓扑、里面没有边"。这两者的调查范围完全不同。
    """
    if topology is None:
        return None
    try:
        services = sorted(topology.services())
        edges = sorted(
            (service, dependency)
            for service in services
            for dependency in topology.dependencies(service)
        )
    except AttributeError:
        return None
    return _digest({"services": services, "edges": edges})


def run_snapshot(
    *,
    budget: Any | None = None,
    scope_depth: int | None = None,
    topology: Any | None = None,
    prompt_version: str | None = None,
    prompt_parts: list[str] | tuple[str, ...] | None = None,
    datasource_health: list[dict[str, Any]] | None = None,
    config_version: str | None = None,
    alert_summary_present: bool | None = None,
    retry_of: str | None = None,
    known_unavailable: list[str] | None = None,
    failed_sources: list[str] | None = None,
    strategy_change: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """本次运行的实际输入。这些值变了**不代表策略变了**。"""
    snapshot: dict[str, Any] = {
        "scope_depth": scope_depth,
        "topology_digest": topology_digest(topology),
        "prompt_version": prompt_version,
        # 逐片段的版本：只给总哈希时，看到结论变化最多只能说"提示词变了"，
        # 说不出是哪一段变的（边界定义 / few-shot / 输出契约）。
        "prompt_parts": list(prompt_parts or ()),
        # 数据源健康是**运行输入**（外部世界的状态），不是策略；
        # 放在快照里是因为"这次为什么证据少"必须能从报告本身回答。
        "datasource_health": list(datasource_health or ()),
        "config_version": config_version,
        # 只记"有没有告警原文"，不记原文本身：快照会进报告，
        # 报告会进证据库与日志，原文属于告警数据、不该复制到第三个地方。
        "alert_summary_present": alert_summary_present,
        # 这一轮在重做哪一轮、上一轮缺了哪些源、这一轮自己又缺了哪些源。
        # 三个值放一起才能回答"重试有没有起作用"：只记 retry_of，
        # 会分不清"源恢复了但结论仍不足"与"同一个源仍然不通"。
        "retry_of": retry_of,
        "known_unavailable": list(known_unavailable or ()),
        "failed_sources": list(failed_sources or ()),
        # 本轮相对上一轮改了什么策略（点分路径）。结论变了而这里非空时，
        # 第一嫌疑是"换了尺子"，不是"系统变了"。
        "strategy_change": strategy_change,
    }
    if budget is not None:
        snapshot["budget"] = {
            name: getattr(budget, name, None)
            for name in ("max_tool_calls", "max_rounds", "max_tokens", "deadline_seconds")
        }
    return snapshot
