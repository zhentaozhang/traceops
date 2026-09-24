"""InvestigationProfile：调查策略的不可变、可 digest 打包。

## 为什么需要（三个真实缺口，不是重构洁癖）

调查策略目前分散在四个文件：

| 位置 | 内容 |
|---|---|
| `orchestration/graph.py:_PLAN_TEMPLATES` | 采集哪些查询模板 |
| `orchestration/detectors.py` | 异常判定阈值 |
| `orchestration/graph.py` | scope_depth / impact_scope |
| `rca/ranking.py:_W_*` | 排序权重 |

这带来三个具体后果：

**1. 消融无法机器归因。** `ablation.diff_arms` 只能比较**声明**的能力位。
"E4 与 E5 只差语义闸门"这句话，没有任何东西能证明阈值、模板、权重也一致。
有人调了 `ERROR_RATE_THRESHOLD` 之后，E4/E5 的差异就不再可归因 —— 而没有人会发现。

**2. 多轮结论无法归因。** `FindingHistorySnapshot` 存了结论，但**没存策略**。
同一事故两次调查结论不同时，答不出"是策略变了还是数据变了"。

**3. 调阈值没有安全入口。** SRE 想调阈值只能改源码重新部署 ——
没有记录、没有版本、无法回滚。

## 设计取舍

| 决定 | 理由 |
|---|---|
| **frozen dataclass** | 策略是配置，不该在中途被改；不可变才能安全共享与哈希 |
| **digest 只覆盖语义内容** | 不含构造时间/来源路径，否则同样策略会得到不同 digest |
| **默认值从代码常量读** | 避免"profile 里写 0.05、代码里写 0.06"的双份真相 |
| **不引入 YAML / 热加载** | profile 是代码内对象；加载/持久化是它之后的事 |

## 边界（诚实声明）

本轮只是**集中并让策略可被证明一致**，不改变任何阈值。
`DEFAULT_PROFILE` 必须复现当前行为 —— 有测试逐项比对代码常量。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class PlanTemplate:
    """一条采集模板：查什么工具、哪个数据源、产出什么证据类型。"""

    tool_name: str
    source_id: str
    evidence_type: str
    template_id: str


def _default_templates() -> tuple[PlanTemplate, ...]:
    """从 `graph._PLAN_TEMPLATES` 派生默认模板 —— 单一事实来源，不写第二份。"""
    from traceops.orchestration.graph import _PLAN_TEMPLATES

    return tuple(
        PlanTemplate(tool_name=tool, source_id=source,
                     evidence_type=evidence_type.value, template_id=template)
        for tool, source, evidence_type, template in _PLAN_TEMPLATES
    )


def _code_thresholds() -> dict[str, float]:
    from traceops.orchestration.detectors import (
        ERROR_RATE_THRESHOLD, SLOW_SPAN_THRESHOLD_SECONDS,
    )

    return {
        "error_rate_threshold": ERROR_RATE_THRESHOLD,
        "slow_span_threshold_seconds": SLOW_SPAN_THRESHOLD_SECONDS,
    }


def _code_weights() -> dict[str, int]:
    from traceops.rca import ranking

    return {
        "w_demote": ranking._W_DEMOTE,
        "w_anomaly_before": ranking._W_ANOMALY_BEFORE,
        "w_trace_anomaly": ranking._W_TRACE_ANOMALY,
        "w_config_change": ranking._W_CONFIG_CHANGE,
        "w_contradiction": ranking._W_CONTRADICTION,
        "w_component_specific": ranking._W_COMPONENT_SPECIFIC,
    }


_DEFAULTS = {**_code_thresholds(), **_code_weights()}


@dataclass(frozen=True)
class InvestigationProfile:
    """一次调查的完整策略。

    字段默认值全部取自代码常量（`_DEFAULTS`），避免出现第二份真相。
    """

    # ---- 采集策略 ----
    plan_templates: tuple[PlanTemplate, ...] = field(default_factory=_default_templates)
    scope_depth: int = 1
    impact_scope: bool = True

    # ---- 异常判定阈值 ----
    error_rate_threshold: float = _DEFAULTS["error_rate_threshold"]
    slow_span_threshold_seconds: float = _DEFAULTS["slow_span_threshold_seconds"]

    # ---- 排序权重 ----
    w_demote: int = _DEFAULTS["w_demote"]
    w_anomaly_before: int = _DEFAULTS["w_anomaly_before"]
    w_trace_anomaly: int = _DEFAULTS["w_trace_anomaly"]
    w_config_change: int = _DEFAULTS["w_config_change"]
    w_contradiction: int = _DEFAULTS["w_contradiction"]
    w_component_specific: int = _DEFAULTS["w_component_specific"]

    # ---- 能力开关（与消融臂 E1–E5 一一对应）----
    #
    # 为什么证据引擎也必须是策略的一部分：E4 相对 E3 的唯一增量就是它。
    # 漏掉这个字段会让 E3/E4 得到**相同 digest**，于是"只改了证据引擎"
    # 这个声明无法被证明 —— 归因失效，且比没有 digest 更危险（看起来更可信）。
    enable_model: bool = False
    enable_tools: bool = True
    enable_evidence_engine: bool = True
    enable_semantic_gate: bool = True
    enable_contradiction: bool = True

    def to_dict(self) -> dict:
        """JSON 安全的表示。

        **不含构造时间、来源路径**等非语义元数据 ——
        否则同样的策略在不同时刻会得到不同 digest，比对就失去意义。
        """
        payload = asdict(self)
        # tuple -> list 以便 JSON 序列化；templates 保持为可读结构
        payload["plan_templates"] = [asdict(t) for t in self.plan_templates]
        return payload

    def digest(self) -> str:
        """策略内容的短哈希。

        排序键固定（`sort_keys=True`）且用紧凑分隔符，
        保证同一内容在任何进程、任何版本 Python 下都得到同一 digest。
        """
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    def explain(self) -> str:
        """人可读的单行摘要（进日志/报告，便于事后比对）。"""
        return (
            f"profile={self.digest()} "
            f"err_rate>={self.error_rate_threshold} "
            f"slow_span>={self.slow_span_threshold_seconds}s "
            f"scope_depth={self.scope_depth} "
            f"model={self.enable_model} tools={self.enable_tools} "
            f"evidence_engine={self.enable_evidence_engine} "
            f"semantic_gate={self.enable_semantic_gate} "
            f"contradiction={self.enable_contradiction}"
        )


#: 与当前代码行为完全等价的默认策略。
DEFAULT_PROFILE = InvestigationProfile()
