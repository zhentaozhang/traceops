"""Action Registry（蓝皮书 §9.1）。

原则：不存在"让 LLM 生成 shell 命令"这条路径。
每个可执行动作必须显式注册，并自带：允许的 target 集合、参数 schema、风险级别、
是否需要审批、执行后要校验的条件。

内置注册表**故意为空**：预置写操作等于"默认就能改环境"，这正是要避免的默认值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError

from traceops.remediation.postconditions import PostCondition


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def order(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.value]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    risk: RiskLevel
    requires_approval: bool
    allowed_targets: set[str]
    parameter_schema: dict[str, Any]
    #: 后置条件的默认值。具体计划可以用 `build_plan(post_conditions=...)` 覆盖
    #: —— 阈值属于这次事故（"错误率 0.05"是服务相关的），不该写死在动作定义里。
    post_conditions: list[PostCondition | str] = field(default_factory=list)
    #: 允许的 target 正则。给"任何人都不能预先列出 target"的场景（内置动作）
    #: 留一条路：白名单仍然显式，只是粒度是"形如服务名的 DNS label"。
    allowed_target_pattern: str | None = None


class ActionRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"action {spec.name!r} already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ActionSpec:
        # 未知动作 = 硬失败。绝不回退到"通用执行器"
        return self._specs[name]

    def names(self) -> list[str]:
        return sorted(self._specs)

    def validate_parameters(self, name: str, parameters: dict[str, Any]) -> None:
        spec = self.get(name)
        try:
            Draft202012Validator(spec.parameter_schema).validate(parameters)
        except SchemaValidationError as exc:
            raise ValueError(
                f"invalid parameters for action {name!r}: {exc.message}"
            ) from exc


SERVICE_TARGET_PATTERN = r"[a-z][a-z0-9-]{2,62}"
"""内置动作接受的 target 形状（DNS label 形式的服务名）。"""


def register_builtin_actions(registry: ActionRegistry) -> ActionRegistry:
    """注册两个内置写动作。**必须显式调用** —— `action_registry()` 仍然是空表。

    为什么不做成"默认注册"：预置写操作等于默认就能改环境。这里提供的是
    "想用的时候有一条已经写好、带审批要求、带后置条件的路"，不是"打开就生效"。

    两个动作都 `requires_approval=True`：它们改的是线上副本数/进程，不是
    可回滚的配置开关，误触的代价是容量而不是配置。
    """
    registry.register(ActionSpec(
        name="scale_replicas",
        description="Scale the workload serving a service to a fixed replica count",
        risk=RiskLevel.MEDIUM,
        requires_approval=True,
        allowed_targets=set(),
        allowed_target_pattern=SERVICE_TARGET_PATTERN,
        parameter_schema={
            "type": "object",
            "properties": {
                "replicas": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["replicas"],
            "additionalProperties": False,
        },
        # 回滚就是把副本数写回原值，不需要推断
        post_conditions=[],
    ))
    registry.register(ActionSpec(
        name="restart_component",
        description="Roll the pods of the workload serving a service",
        risk=RiskLevel.HIGH,
        requires_approval=True,
        allowed_targets=set(),
        allowed_target_pattern=SERVICE_TARGET_PATTERN,
        parameter_schema={
            "type": "object",
            "properties": {
                "grace_seconds": {"type": "integer", "minimum": 0, "maximum": 600},
            },
            "required": [],
            "additionalProperties": False,
        },
        post_conditions=[],
    ))
    return registry


def action_registry() -> ActionRegistry:
    """返回空的注册表：调用方必须显式注册它真正允许的动作。"""
    return ActionRegistry()
