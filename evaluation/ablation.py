"""E1–E5 消融实验的实验臂定义与差分校验。

## 为什么需要这个模块（而不只是"等 LLM 接进来再跑"）

`评测口径` 诚实地写着：

> P0 只能跑 E3/E4 的"单臂"版本……要形成真正的消融对比，必须先有 LLM 节点（P1），
> 否则 E1/E2/E5 没有可执行内容。

这个判断是对的，但它同时意味着**消融的框架一直不存在**——连"如何定义一个可比的
实验臂"都没有代码。等模型接进来再临时设计，最容易犯的错是：

**多个变量一起变**。E4 → E5 如果同时改了拓扑深度或预算，那么测出来的差异
不能归因到"假设验证"这个机制，整个消融就失去意义。

所以本模块先把实验臂**声明清楚**，并**机械校验**相邻臂只差一个变量。
这件事不需要 LLM，现在就能做，而且做了之后模型接进来时才有可比的基础。

## 实验臂定义（蓝图 §10.2）

| 臂 | 能力增量 |
|---|---|
| E1 | 无工具、无证据引擎，模型直接看观测摘要 |
| E2 | + MCP 工具（能主动取数） |
| E3 | + 多阶段调查图（确定性节点编排） |
| E4 | + 证据引擎（内容寻址、校验、引用闸门） |
| E5 | + 假设验证（语义覆盖 + 矛盾检测） |

## 诚实边界

E1/E2 需要真实模型。`runnable_arms(has_model=False)` 会**显式排除**它们，
而不是伪造一个"跑过"的结果 —— 把"没有可执行内容"标成"已完成"是本项目
反复强调要避免的学术不诚实。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class AblationArm:
    """一个消融实验臂。

    布尔能力位是**唯一可比的东西**：臂之间的差异必须恰好是这些位的一个变化。
    数值型配置（预算、温度）属于"固定条件"，所有臂共用同一组值。
    """

    arm_id: str
    description: str
    uses_model: bool
    uses_tools: bool
    uses_evidence_engine: bool
    uses_verification: bool
    requires_model: bool = field(default=False)

    def to_dict(self) -> dict:
        return asdict(self)


#: 五个实验臂。顺序即能力递增顺序。
ARMS: dict[str, AblationArm] = {
    "E1": AblationArm(
        arm_id="E1",
        description="LLM 直接分析观测摘要，无工具、无证据引擎",
        uses_model=True, uses_tools=False,
        uses_evidence_engine=False, uses_verification=False,
        requires_model=True,
    ),
    "E2": AblationArm(
        arm_id="E2",
        description="LLM + MCP 工具，但无证据引擎",
        uses_model=True, uses_tools=True,
        uses_evidence_engine=False, uses_verification=False,
        requires_model=True,
    ),
    "E3": AblationArm(
        arm_id="E3",
        description="多阶段调查图（plan→collect→hypothesize→link→correlate→rank→report），全部确定性节点",
        uses_model=False, uses_tools=True,
        uses_evidence_engine=False, uses_verification=False,
    ),
    "E4": AblationArm(
        arm_id="E4",
        description="E3 + 证据引擎（内容寻址 checksum、四类确定性校验、引用完整性闸门）",
        uses_model=False, uses_tools=True,
        uses_evidence_engine=True, uses_verification=False,
    ),
    "E5": AblationArm(
        arm_id="E5",
        description="E4 + 假设验证（语义覆盖率闸门 + 确定性矛盾检测）",
        uses_model=False, uses_tools=True,
        uses_evidence_engine=True, uses_verification=True,
    ),
}

#: 能力位名称（差分比较只在这些维度上做）。
_CAPABILITY_FIELDS = ("uses_model", "uses_tools", "uses_evidence_engine", "uses_verification")

#: 所有臂共用的固定条件。蓝图 §10.2 要求正式实验前锁定这些值。
_FIXED_CONDITIONS: dict[str, object] = {
    "model_temperature": 0.0,
    "max_tool_calls": 40,
    "topology_depth": 2,
    "prompt_version": "p1-v1",
    "tool_schema_version": "1.0",
    "repeat_count": 5,
}


def diff_arms(from_arm: str, to_arm: str) -> list[str]:
    """两个实验臂之间**变了哪些能力位**。

    返回字段名列表（不含固定条件）。空列表表示两个臂在能力上等强 ——
    那意味着这不是一个有效的消融对比。
    """
    if from_arm not in ARMS or to_arm not in ARMS:
        raise KeyError(f"unknown arm: {from_arm!r} or {to_arm!r}")
    left, right = ARMS[from_arm], ARMS[to_arm]
    return [name for name in _CAPABILITY_FIELDS if getattr(left, name) != getattr(right, name)]


def arm_profile(arm_id: str):
    """把实验臂渲染成 **InvestigationProfile**（而不是零散的能力位字典）。

    为什么必须走 profile：`arm_config` 只能声明能力位，无法证明
    阈值/模板/权重在各臂之间一致。一旦有人调了阈值，
    E4/E5 的差异就不再可归因 —— 而**没有人会发现**。
    profile 的 digest 把这个假设变成可校验的事实。
    """
    from dataclasses import replace

    from evaluation.investigation_profile import DEFAULT_PROFILE

    if arm_id not in ARMS:
        raise KeyError(f"unknown arm: {arm_id!r}")
    arm = ARMS[arm_id]
    return replace(
        DEFAULT_PROFILE,
        enable_model=arm.uses_model,
        enable_tools=arm.uses_tools,
        enable_evidence_engine=arm.uses_evidence_engine,
        enable_semantic_gate=arm.uses_verification,
        enable_contradiction=arm.uses_verification,
    )


def arm_config(arm_id: str) -> dict:
    """把一个实验臂渲染成可传给 harness 的配置。

    包含能力位 + **固定条件**。固定条件必须显式出现，否则"同一环境"
    只停留在口头承诺 —— 两次运行温度不同，差异就无法归因。
    """
    if arm_id not in ARMS:
        raise KeyError(f"unknown arm: {arm_id!r}")
    arm = ARMS[arm_id]
    config = dict(_FIXED_CONDITIONS)
    config.update({
        "arm_id": arm.arm_id,
        "uses_model": arm.uses_model,
        "uses_tools": arm.uses_tools,
        "evidence_engine": arm.uses_evidence_engine,
        "uses_verification": arm.uses_verification,
        # 与 RunContext 的实际字段对齐：E5 才启用语义/矛盾校验
        "evidence_engine": arm.uses_evidence_engine,
        "enable_semantic_gate": arm.uses_verification,
        "enable_contradiction": arm.uses_verification,
        # 策略 digest：运行时据此**证明**各臂用的是同一套阈值/模板/权重，
        # 而不是只有声明。
        "profile_digest": arm_profile(arm_id).digest(),
    })
    return config


def runnable_arms(*, has_model: bool) -> set[str]:
    """在当前环境下**真正可运行**的实验臂。

    没有模型时排除 E1/E2 —— 它们没有可执行内容。这不是保守，
    而是拒绝把"跑不了"包装成"跑过"。E5 不需要模型（它是确定性校验器增强），
    所以不受 has_model 影响。
    """
    return {
        arm_id for arm_id, arm in ARMS.items()
        if not arm.requires_model or has_model
    }


def arm_matrix() -> list[dict]:
    """完整的实验矩阵（供报告与文档使用）。"""
    return [
        {
            "arm_id": arm.arm_id,
            "description": arm.description,
            "requires_model": arm.requires_model,
            **{name: getattr(arm, name) for name in _CAPABILITY_FIELDS},
            "diff_from_previous": (diff_arms(previous, arm.arm_id) if previous else []),
        }
        for previous, arm in zip([None, "E1", "E2", "E3", "E4"],
                                 [ARMS[k] for k in ("E1", "E2", "E3", "E4", "E5")])
    ]
