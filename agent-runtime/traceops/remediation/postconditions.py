"""后置条件的**可执行**形式。

## 被替换的缺陷

`ActionSpec.post_conditions` 一直是 `list[str]`：`["error_rate_below_threshold"]`。
`execute_plan` 对它的处理是 `if plan.post_conditions: verifier.verify(plan)` ——
也就是说**只要这个列表非空**，就会去问一个"验证器"，而验证器至今没有任何
具体实现。整条"执行完要看结果"的链路在类型上就断了：

  - 字符串没有人能执行，它只是给读者看的注释；
  - 于是"恢复了吗"这个问题从来没有被真正问过。

这里把条件变成数据：种类、对象、阈值、观察窗口。能自动判定的只有一种
（错误率低于阈值，直接问 Prometheus 适配器）；其余一律显式标成
`manual_check` —— **不认识就是不可验证，不是默认通过**。

## 为什么不能只有"低于阈值"

"错误率低于 0.05"能回答"好了吗"，不能回答"是不是更糟了"。所以验证还要
一个基线：计划构造时把当时的观测值记进 `RemediationPlan.baseline`，
执行后既看阈值也看相对基线 —— 超过基线才是 WORSE（这个动作让事情变坏了），
仅仅没达标只是 UNCHANGED。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ERROR_RATE_BELOW = "error_rate_below"
"""错误率低于阈值。唯一能自动判定的种类。"""

MANUAL_CHECK = "manual_check"
"""需要人看的条件。**永远不会被自动判成通过**。"""

KNOWN_KINDS = frozenset({ERROR_RATE_BELOW, MANUAL_CHECK})


@dataclass(frozen=True)
class PostCondition:
    kind: str
    service: str | None = None
    threshold: float | None = None
    window_seconds: int = 300
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KNOWN_KINDS:
            raise ValueError(
                f"unknown post-condition kind {self.kind!r}; known: {sorted(KNOWN_KINDS)}")
        if self.window_seconds <= 0:
            raise ValueError(f"window_seconds must be positive, got {self.window_seconds}")
        if self.kind == ERROR_RATE_BELOW:
            if not self.service:
                raise ValueError("error_rate_below requires a service")
            if self.threshold is None or self.threshold < 0:
                raise ValueError("error_rate_below requires a non-negative threshold")
        if self.kind == MANUAL_CHECK and not self.note:
            raise ValueError("manual_check requires a note explaining what a person must confirm")

    # ---- 构造 ----

    @classmethod
    def error_rate_below(cls, service: str, threshold: float, *,
                         window_seconds: int = 300) -> "PostCondition":
        return cls(kind=ERROR_RATE_BELOW, service=service, threshold=threshold,
                   window_seconds=window_seconds)

    @classmethod
    def manual_check(cls, note: str) -> "PostCondition":
        return cls(kind=MANUAL_CHECK, note=note)

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "window_seconds": self.window_seconds}
        if self.service is not None:
            data["service"] = self.service
        if self.threshold is not None:
            data["threshold"] = self.threshold
        if self.note:
            data["note"] = self.note
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PostCondition":
        return cls(
            kind=data["kind"],
            service=data.get("service"),
            threshold=data.get("threshold"),
            window_seconds=int(data.get("window_seconds", 300)),
            note=data.get("note", ""),
        )

    def describe(self) -> str:
        if self.kind == ERROR_RATE_BELOW:
            return (f"error rate of {self.service} below {self.threshold} "
                    f"over {self.window_seconds}s")
        return f"manual check: {self.note}"


def as_condition(item: Any) -> PostCondition | None:
    """把计划里的后置条件规范成 `PostCondition`；自由文本返回 None。

    自由文本**不是**"无需验证"，是"无法验证" —— 调用方必须区分这两件事，
    所以这里返回 None 而不是造一个永远为真的条件。
    """
    if isinstance(item, PostCondition):
        return item
    if isinstance(item, dict):
        return PostCondition.from_dict(item)
    return None
