"""影子判定：把"闸门本来会怎么判"记下来，但不照着做。

## 为什么要这个

我们的三个弃权闸门（同分歧义 / 语义覆盖率为 0 / 采集不全时的反驳）都是
**静默降级**：结论从 SUPPORTED 变成 INCONCLUSIVE，报告里只留一句人类可读的
摘要。于是出现两个问题：

1. **无法测量**：想知道"语义闸门到底有多频繁地拦下了本来可以给出的结论"，
   只能翻历史报告数 `diagnosis_status` 的分布 —— 而那个分布已经是被闸门
   改写过的结果，拦下了多少次**根本无从得知**。
2. **无法灰度**：想收紧或放宽某个闸门，唯一办法是改代码再全量重跑。
   没有"先观察一段时间、看它要拦几次、再决定要不要真的拦"的中间档。

super-agent 的解法是 `AcceptMode = off | observe | auto`：observe 模式下
系统照常做判断、照常记 `would_accept`，但**不执行**那个动作。把同一个思路
搬过来：闸门照常算，把"本该判成什么、因为什么"写进报告的影子区，
真实结论保持不动。

## 为什么不干脆做成开关矩阵

开关（`enable_semantic_gate` 那套）只回答"开不开"；影子模式回答
"如果开，会拦掉什么"。前者用于消融实验，后者用于线上校准 —— 两件事。

区别在于：消融实验是**先关掉再对比**，影子模式是**同时得到两个结果**。
线上只有一次机会，关掉闸门跑一周就等于一周没有闸门保护。
"""
from __future__ import annotations

from dataclasses import dataclass


#: 影子判定的原因码。
#:
#: 用码而不是自由文本：报告要能按原因聚合统计（"语义闸门这个月拦了 40 次，
#: 其中 32 次是 latency_degradation 但没有慢 span"），自由文本做不到。
SHADOW_REASONS: dict[str, str] = {
    "ambiguous_tie": "多个不同服务的候选同分，规则无法裁决",
    "semantic_coverage_zero": "首选候选的预期观测一个都没出现",
    "refuted_but_incomplete": "全部候选被反驳，但采集不完整",
    "insufficient_evidence": "证据不足，无法给出确定结论",
    "model_out_of_scope": "模型提出的服务不在证据或拓扑范围内",
    "model_incomplete": "模型输出缺少 candidate_service 或 fault_type",
}


@dataclass(frozen=True)
class ShadowDecision:
    """一条"本来会这么判、但这次没这么判"的记录。"""

    #: 哪个闸门做的判定（`semantic_gate` / `ambiguity_gate` / ...）
    gate: str
    #: 闸门生效时**本该**得出的结论。
    would_be: str
    #: `SHADOW_REASONS` 里的键。
    reason: str
    #: 人类可读的补充说明（含具体候选/覆盖项）。
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "would_be": self.would_be,
            "reason": self.reason,
            "reason_text": SHADOW_REASONS.get(self.reason, self.reason),
            "detail": self.detail,
        }
