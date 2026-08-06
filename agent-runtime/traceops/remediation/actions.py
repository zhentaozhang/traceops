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
    post_conditions: list[str] = field(default_factory=list)


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


def action_registry() -> ActionRegistry:
    """返回空的注册表：调用方必须显式注册它真正允许的动作。"""
    return ActionRegistry()
