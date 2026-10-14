"""入站事件的契约校验（Python 侧）。

## 为什么生产代码也要校验契约

`contracts/` 里的 JSON Schema 一直存在，但**两侧的生产代码都不用它**：
Java 侧只在两个测试类里用，Python 侧一次都不用。结果是三方（schema / Java
生产者 / Python 消费者）可以长期各自演化而无人察觉。

已经发生过：人工创建的调查传 `priority="manual"`，而 schema 的 enum 只有
`low/normal/high`。这条事件在控制面照常组装、照常入 outbox、照常投递，
Python 侧 `payload.get("priority", "normal")` 照常读出一个没人认识的值，
**整条链路上没有任何一处报错**。

## 为什么在消费入口而不是在更深处

`derive_request_from_event` 是入站消息唯一的解释点：所有后续行为都由它
产出的 `InvestigationRequest` 决定。校验放在这里，坏消息在第一跳就变成
`investigation.invalid`，而不是等到某个字段在 200 行之外引发奇怪的
`KeyError`。

## schema 从哪来

仓库根目录的 `contracts/`（唯一事实来源，与 Java 侧复制的是同一批文件）。
路径解析顺序：

1. 环境变量 `TRACEOPS_CONTRACTS_DIR`
2. 从本文件向上逐级找 `contracts/events/v1/`
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

#: 事件类型 → schema 相对路径。新增事件类型时这里同步加一行。
_EVENT_SCHEMA_PATHS: dict[str, str] = {
    "investigation.requested": "events/v1/investigation-requested.schema.json",
    "investigation.updated": "events/v1/investigation-updated.schema.json",
    "investigation.completed": "events/v1/investigation-completed.schema.json",
    "investigation.failed": "events/v1/investigation-failed.schema.json",
}

_SCHEMA_FILES: tuple[str, ...] = (
    "common/event-envelope.schema.json",
    "common/time-range.schema.json",
    "events/v1/investigation-requested.schema.json",
    "events/v1/investigation-updated.schema.json",
    "events/v1/investigation-completed.schema.json",
    "events/v1/investigation-failed.schema.json",
)


class ContractViolation(ValueError):
    """事件消息与 `contracts/` 里的 JSON Schema 不符。

    继承 `ValueError` 是有意的：worker 的入站处理路径已经把所有
    `ValueError` 当成"这条消息不能处理"并写 `investigation.invalid` +
    死信。契约违规只是更精确的一种 ValueError，不该引入第二条错误路径。
    """

    def __init__(self, event_type: str, violations: list[str], *,
                 missing_envelope: bool = False) -> None:
        self.event_type = event_type
        self.violations = tuple(violations)
        self.missing_envelope = missing_envelope
        detail = "; ".join(violations) if violations else "unknown"
        # 前缀是有意的：`deadletter.classify_failure` 按文本归类，
        # "缺信封"和"信封在但内容不符"要落进不同类别 —— 前者说明生产方
        # 根本没按 {envelope, payload} 写，后者说明字段含义对不上。
        prefix = "envelope is missing from the event; contract violation" \
            if missing_envelope else "contract violation"
        super().__init__(
            f"{prefix} (event {event_type!r}, "
            f"{len(self.violations)} violation(s)): {detail}")


def contracts_root() -> Path:
    """定位仓库根目录的 `contracts/`。"""
    override = os.environ.get("TRACEOPS_CONTRACTS_DIR")
    if override:
        root = Path(override).expanduser().resolve()
        if not (root / "events" / "v1").is_dir():
            raise FileNotFoundError(
                f"TRACEOPS_CONTRACTS_DIR does not look like a contracts dir: {root}")
        return root

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts"
        if (candidate / "events" / "v1").is_dir():
            return candidate
    raise FileNotFoundError(
        "cannot locate the contracts/ directory; set TRACEOPS_CONTRACTS_DIR")


@lru_cache(maxsize=1)
def _registry_and_validators() -> tuple[Registry, dict[str, Draft202012Validator]]:
    """加载全部 schema 并编译校验器（进程内只做一次）。"""
    root = contracts_root()
    registry = Registry()
    documents: dict[str, Any] = {}
    for relative in _SCHEMA_FILES:
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"contract schema is missing: {path}")
        document = json.loads(path.read_text(encoding="utf-8"))
        documents[relative] = document
        registry = registry.with_resource(
            document["$id"],
            Resource.from_contents(document, default_specification=DRAFT202012),
        )

    validators: dict[str, Draft202012Validator] = {}
    for event_type, relative in _EVENT_SCHEMA_PATHS.items():
        validators[event_type] = Draft202012Validator(
            documents[relative], registry=registry)
    return registry, validators


def validate_event(event_type: str | None, message: dict[str, Any]) -> None:
    """校验一条完整的 `{envelope, payload}` 消息。

    @param event_type 期望的事件类型；`None` 时取 `envelope.event_type`
    @raises ContractViolation 消息与契约不符
    """
    _registry, validators = _registry_and_validators()
    envelope = message.get("envelope")
    resolved = event_type
    if resolved is None and isinstance(envelope, dict):
        candidate = envelope.get("event_type")
        if isinstance(candidate, str):
            resolved = candidate

    if resolved not in validators:
        raise ContractViolation(str(resolved), [
            f"no schema registered for event type {resolved!r} "
            f"(known: {', '.join(sorted(validators))})"],
            missing_envelope=not isinstance(envelope, dict) or not envelope)

    errors = sorted(
        validators[resolved].iter_errors(message),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        raise ContractViolation(resolved, [_describe(error) for error in errors],
                                missing_envelope=not isinstance(envelope, dict) or not envelope)


def _describe(error: Any) -> str:
    path = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{path}: {error.message}"


def schema_count() -> int:
    """已登记的事件 schema 数量（供测试确认不是空校验器）。"""
    _registry, validators = _registry_and_validators()
    return len(validators)
