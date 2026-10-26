"""把"答对了/答错了"拆成三步，并给出两条不需要智能的基线。

## 为什么要拆

一个分数无法告诉我们在哪一步输掉。诊断链路其实有三步，每一步都可能输：

1. **观测到了吗**（`observed`）：真实根因服务的证据是否进了采集结果。
   输在这里是数据源/约定/窗口问题，再强的排序也救不回来。
2. **进候选了吗**（`in_candidates`）：假设生成有没有把它列出来。
   输在这里是检测器/归因表的问题。
3. **排第一了吗**（`top1`，即 AC@1）：进去之后有没有被放到第一位。
   输在这里才是"推理"问题。

RCAEval 的 `--report-decomposition`（Retrieval@K / Rerank@1）是同一思路：
先问"真因在不在候选集里"，再问"排序对不对"。没有这层拆分，一次 miss 会被
笼统记成"诊断错误"，而三种 miss 的修法完全不同。

## 为什么要有"不需要智能"的基线

对随机基线（`chance.py`）算 lift 太便宜：把候选池从 4 个缩小，lift 自动变大。
真正的对手是**一眼就能写出来的启发式**：

- `peak`：谁的最大指标值最高，就怪谁（不看时间、不看日志、不看拓扑）
- `earliest`：谁最早出现异常，就怪谁（不看幅度）

如果我们的规则排序赢不过这两条，那么"逐跳可审计的证据推理"这个卖点就没有
体现在结果上。输赢都要写进报告。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from traceops.domain.models import EvidenceType


@dataclass(frozen=True)
class Funnel:
    """一次诊断在三步链路里的位置。任一步为 False，后续步骤必然为 False。"""

    observed: bool
    in_candidates: bool
    top1: bool
    rank: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed": self.observed,
            "in_candidates": self.in_candidates,
            "top1": self.top1,
            "rank": self.rank,
        }


def _attributed(evidence) -> str:
    from evaluation.metrics import _attributed_service

    return (_attributed_service(evidence) or "").strip().lower()


def _normalised(evidence) -> dict[str, Any]:
    data = getattr(evidence, "normalized_data", None)
    return data if isinstance(data, dict) else {}


def _peak_of(evidence) -> float | None:
    from traceops.orchestration.graph import _metric_peak

    try:
        return _metric_peak(_normalised(evidence))
    except (TypeError, ValueError, KeyError):
        return None


def funnel(report, wanted_service: str) -> Funnel:
    """真实根因服务走完整条链路了吗。"""
    wanted = wanted_service.strip().lower()
    evidence = list(getattr(report, "evidence", None) or [])
    hypotheses = list(getattr(report, "hypotheses", None) or [])

    observed = any(_attributed(item) == wanted for item in evidence)
    ranks = [
        h.rank
        for h in hypotheses
        if (getattr(h, "candidate_service", "") or "").strip().lower() == wanted
        and getattr(h, "rank", None) is not None
    ]
    in_candidates = bool(ranks)
    rank = min(ranks) if ranks else None
    return Funnel(observed=observed, in_candidates=in_candidates, top1=rank == 1, rank=rank)


def peak_pick(report) -> str | None:
    """启发式一：最大指标峰值最高的服务。指标不可用时退回 None（不是猜）。"""
    best_service: str | None = None
    best_value: float | None = None
    for item in getattr(report, "evidence", None) or []:
        if getattr(item, "evidence_type", None) is not EvidenceType.METRIC:
            continue
        value = _peak_of(item)
        if value is None:
            continue
        if best_value is None or value > best_value:
            best_value, best_service = value, _attributed(item)
    return best_service or None


def earliest_pick(report) -> str | None:
    """启发式二：最早出现观测异常的服务（不含变更事件）。

    **按服务聚合，不按证据行取最小。** 一条指标、一条日志、一个 span 常常落在
    同一个采样时刻上，而 `Evidence.id` 是随机 uuid —— 直接对证据行取最小会让
    这条基线在 F-001 这类场景里**每次运行给出不同的答案**，于是"基线 0.64"
    这个数字本身不可复现。同一时刻多个服务时按服务名排序（确定性优先，
    这个基线本来就分辨不出来，不该假装能）。
    """
    first_seen: dict[str, Any] = {}
    for item in getattr(report, "evidence", None) or []:
        if getattr(item, "evidence_type", None) is EvidenceType.EVENT:
            continue
        service = _attributed(item)
        if not service:
            continue
        when = getattr(item, "observed_at", None)
        if when is None:
            continue
        if service not in first_seen or when < first_seen[service]:
            first_seen[service] = when
    if not first_seen:
        return None
    earliest = min(first_seen.values())
    leaders = sorted(service for service, when in first_seen.items() if when == earliest)
    if len(leaders) > 1:
        # 多个服务同时最先出现异常 → 这条基线分辨不出来。返回 None（不参与
        # 打分），而不是靠字母序硬挑一个：硬挑会让"基线 0.36"变成
        # "按字母序选出来的 0.36"，既不可复现也不可解释。
        return None
    return leaders[0]


def heuristic_picks(report) -> tuple[str | None, str | None]:
    return peak_pick(report), earliest_pick(report)


def baseline_comparison(outcomes) -> dict[str, Any]:
    """在**同一批场景**上比较"我们"与"朴素基线"。

    为什么不能直接比两个总均值：基线的分母比我们小（它在并列/无指标时弃权）。
    0.44（9 个场景）对 0.91（11 个场景）不是同一件事。这里只回答一个问题：
    *在基线愿意表态的那些场景上，谁对得更多？* 我们的优势如果只来自"覆盖了
    基线弃权的场景"，这个函数会把它暴露出来。
    """
    rows = [o for o in outcomes if getattr(o, "fault_injected", False)]
    result: dict[str, Any] = {}
    for name, attr in (("peak_metric", "heuristic_peak_top1"),
                       ("earliest_anomaly", "heuristic_earliest_top1")):
        decided = [o for o in rows if getattr(o, attr) is not None]
        ours = sum(1 for o in decided if o.service_ac_at_1)
        theirs = sum(1 for o in decided if getattr(o, attr))
        result[name] = {
            "decided": len(decided),
            "ours_at_1": round(ours / len(decided), 4) if decided else 0.0,
            "theirs_at_1": round(theirs / len(decided), 4) if decided else 0.0,
            "abstained": len(rows) - len(decided),
        }
    return result
