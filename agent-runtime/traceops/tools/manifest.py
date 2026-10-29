"""数据源清单：一个源能不能查、缺了它报告会说什么，都应该是声明出来的。

为什么需要它
------------
现在"这个源能不能查"分散在三处，而且没有一处是权威的：

* `DatasourceAdapters.capabilities` —— 有没有装适配器（运行时事实）；
* `evidence/templates.py` 的约定 —— 名字对不对得上（查询能不能成立）；
* `_profile_items` 里硬编码的 `"profile"` 字面量 —— 唯一一处显式开关。

后果是两个方向的错：

1. **该查的没查**：装了个只读 Prometheus 的部署，计划里照样有 Loki/Tempo 两项，
   每次调查都多两条 `missing_data`。噪声会被当成"数据缺失"进报告，稀释真正的缺失。
2. **查不到的还查**：接进来的集群没有 `http_requests_total`，查询返回空，
   我们把它记成"查了，没有"（阴性证据）—— 而真相是"这个集群根本不产这类指标"。
   前者能支持 `REFUTED`，后者不能。

清单要回答的就是这两件事，每个源四条：

| 字段 | 回答什么 |
| --- | --- |
| `capability` | 它属于哪类证据（metric / log / trace / profile / event） |
| `required` | 缺了它，结论还成不成立（`optional=False` 的源缺失会进报告的"取证缺口"） |
| `preconditions` | 要让查询成立，集群里必须有什么（指标名、标签名） |
| `when_absent` | 这个源缺失时，报告该怎么说（一句人话，进 `datasource_gaps`） |

刻意的取舍
----------
**不确定就按"能查"处理。** 适配器没有声明 `capabilities` 时（测试替身、老代码），
metic/log/trace 仍然进计划 —— 与改动前完全一致。只有**显式声明**的能力表才被当真。
`profile` 是唯一例外：它是新增能力，必须显式声明（否则每次调查都多一条噪声）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 已知的能力名。与 `EvidenceType` 对齐，另外多一个 `event`（变更流水，不走数据源）。
CAPABILITIES = ("metric", "log", "trace", "profile", "event")

#: 基线证据类别：缺了它们，报告必须明说"这类证据不可能出现在这里"。
#:
#: 剖析不在其中 —— 它是可选加深项，缺了不影响结论成立与否（见下面的默认清单）。
BASELINE_CAPABILITIES = ("metric", "log", "trace")

#: 默认清单 = 演示栈：Prometheus / Loki / Tempo 必备，Pyroscope 可缺。
_DEFAULTS: tuple[tuple[str, str, bool, tuple[str, ...], str], ...] = (
    (
        "prometheus", "metric", True,
        ("http_requests_total 带 service 与 status 标签",),
        "指标源缺失：错误率与峰值都无法观测，结论只能来自日志与 trace",
    ),
    (
        "loki", "log", True,
        ("日志带 service 与 level 标签",),
        "日志源缺失：连接池/超时这类只有日志能看出来的机制无法确认",
    ),
    (
        "tempo", "trace", True,
        ("span 带 service.name 属性",),
        "trace 源缺失：慢在哪里、上下游边界都无法确认",
    ),
    (
        "pyroscope", "profile", False,
        ("服务暴露持续剖析端点",),
        "剖析源未配置：热点函数不可见（这只影响 deepen 阶段，不影响结论是否成立）",
    ),
)


class ManifestError(ValueError):
    """清单本身写错了。宁可启动时报错，也不要跑到一半才发现某个源永远查不到。"""


def _as_tuple(value: Any, *, field_name: str, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError(f"{source}.{field_name} must be a list of strings")
    return tuple(value)


@dataclass(frozen=True)
class SourceSpec:
    """一个数据源的声明。"""

    name: str
    capability: str
    required: bool = True
    preconditions: tuple[str, ...] = ()
    when_absent: str = ""

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES:
            raise ManifestError(
                f"{self.name}: unknown capability {self.capability!r} "
                f"(known: {', '.join(CAPABILITIES)})"
            )
        if not self.when_absent.strip():
            # 空字符串会被渲染成 "loki: " 一行，看着像故障但什么也没说。
            raise ManifestError(
                f"{self.name}: 'when_absent' must say what the report should claim "
                f"when this source is missing")
        if not self.name.strip():
            raise ManifestError("a source needs a name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capability": self.capability,
            "required": self.required,
            "preconditions": list(self.preconditions),
            "when_absent": self.when_absent,
        }


@dataclass(frozen=True)
class DatasourceManifest:
    """一组数据源声明。`sources` 为空 = 一个源都不许查（不是"全都允许"）。"""

    sources: Mapping[str, SourceSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, spec in self.sources.items():
            if name != spec.name:
                raise ManifestError(f"manifest key {name!r} does not match source name {spec.name!r}")

    # -- 查询 -------------------------------------------------------------
    def get(self, name: str) -> SourceSpec | None:
        return self.sources.get(name)

    def by_capability(self, capability: str) -> tuple[SourceSpec, ...]:
        return tuple(spec for spec in self.sources.values() if spec.capability == capability)

    def names(self) -> tuple[str, ...]:
        return tuple(self.sources)

    def capabilities_in_use(self) -> tuple[str, ...]:
        """清单里真正被声明过的能力类别（供工具注册表裁剪）。"""
        return tuple(dict.fromkeys(spec.capability for spec in self.sources.values()))

    def absent_baseline_capabilities(self) -> tuple[str, ...]:
        """一条源都没声明的基线证据类别。

        与 `gaps()` 是两件事，容易混：

        * `gaps()` —— **声明了却用不了**（清单与部署漂移了，是配置错误）；
        * 这个方法 —— **压根没声明**（这个部署就是没有日志）。后者不是故障，
          但读者会默认"没提到就是查过了没事"，所以报告必须把它说出来。
        """
        return tuple(cap for cap in BASELINE_CAPABILITIES if not self.supports(cap))

    def supports(self, capability: str) -> bool:
        return any(spec.capability == capability for spec in self.sources.values())

    # -- 与运行时事实对账 -------------------------------------------------
    def gaps(self, present: Iterable[str] | None) -> list[dict[str, Any]]:
        """清单里有、但运行时没有的源（按能力名对账）。

        `present=None` 表示"运行时没有声明能力表"—— 这时**不猜**：
        返回空列表，因为把"不知道"说成"缺了"会让报告里出现假的取证缺口。
        """
        if present is None:
            return []
        present = set(present)
        gaps: list[dict[str, Any]] = []
        for spec in self.sources.values():
            if spec.capability in present:
                continue
            gaps.append({
                "source": spec.name,
                "capability": spec.capability,
                "required": spec.required,
                "when_absent": spec.when_absent,
            })
        return gaps

    def describe(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self.sources.values()]


def default_manifest() -> DatasourceManifest:
    return DatasourceManifest(
        sources={
            name: SourceSpec(
                name=name, capability=capability, required=required,
                preconditions=preconditions, when_absent=when_absent,
            )
            for name, capability, required, preconditions, when_absent in _DEFAULTS
        }
    )


def manifest_from_dict(payload: Any) -> DatasourceManifest:
    """从 JSON 形状构造。未知字段直接拒绝 —— 拼错的键会让声明静默失效。"""
    if not isinstance(payload, Mapping):
        raise ManifestError("manifest must be an object with a 'sources' object")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, Mapping):
        raise ManifestError("manifest.sources must be an object keyed by source name")
    known = {"name", "capability", "required", "preconditions", "when_absent"}
    sources: dict[str, SourceSpec] = {}
    for name, raw in raw_sources.items():
        if not isinstance(raw, Mapping):
            raise ManifestError(f"sources.{name} must be an object")
        unknown = set(raw) - known
        if unknown:
            raise ManifestError(
                f"sources.{name}: unknown field(s) {sorted(unknown)} (known: {sorted(known)})")
        sources[str(name)] = SourceSpec(
            name=str(raw.get("name") or name),
            capability=str(raw.get("capability") or ""),
            required=bool(raw.get("required", True)),
            preconditions=_as_tuple(raw.get("preconditions"), field_name="preconditions", source=str(name)),
            when_absent=str(raw.get("when_absent") or ""),
        )
    return DatasourceManifest(sources=sources)


def load_manifest(path: str | Path) -> DatasourceManifest:
    return manifest_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def available_capabilities(adapters: Any) -> frozenset[str] | None:
    """适配器声明的能力表。`None` 表示它没声明（"不知道"，不是"什么都没有"）。"""
    if adapters is None:
        return None
    declared = getattr(adapters, "capabilities", None)
    if declared is None:
        return None
    return frozenset(str(item) for item in declared)


#: 适配器用的能力词 → 清单用的能力词。
#:
#: 两套词表没必要为了整齐而合并：适配器那一侧是"我能执行哪些取数方法"，
#: 清单这一侧是"这类证据是什么"。合并会让 `DatasourceAdapters.capabilities`
#: 的既有取值（`metric_series`/`error_rate`/`logs`/`traces`/`profile`）变成
#: 破坏性修改，而这个映射只有四处。
ADAPTER_CAPABILITY_TOKENS: dict[str, str] = {
    "metric_series": "metric",
    "error_rate": "metric",
    "logs": "log",
    "traces": "trace",
    "profile": "profile",
}


def present_capabilities(adapters: Any) -> frozenset[str] | None:
    """运行时实际能查的证据类别。`None` = 适配器没声明，别猜。"""
    tokens = available_capabilities(adapters)
    if tokens is None:
        return None
    return frozenset(ADAPTER_CAPABILITY_TOKENS[token] for token in tokens
                     if token in ADAPTER_CAPABILITY_TOKENS)
