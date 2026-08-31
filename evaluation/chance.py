"""机会校正指标：AC@k 必须与候选池大小一起看，否则是伪指标。

## 问题（实测确认）

当前三个场景各自只生成 **1 个候选服务**：

```
F-002 hypotheses: 1  distinct services: 1 -> ['billing-api']
F-003 hypotheses: 1  distinct services: 1 -> ['redis']
F-004 hypotheses: 1  distinct services: 1 -> ['inventory-service']
```

于是 `rank <= 3` / `rank <= 5` **恒真**。报告上的 `AC@3=1.00 AC@5=1.00`
不是"系统能从前 5 名里找到根因"，而是"候选池里只有 1 个东西"。
把它当成能力指标对外说，就是拿一个分母为 1 的比率冒充 Top-5 检索能力。

RCAEval 的做法：报告 `n_candidates`（候选池大小），并给出 Chance@k =
随机从候选池取 k 个能命中的概率（`k / |candidates|`），再算 Lift = AC@k / Chance@k。
只有 Lift > 1 才说明排序真的比随机好。

## 本模块提供

- `candidate_pool_size`：去重后的候选服务数（同一服务多个机制只算 1）
- `chance_at(observation, k)`：随机基线的命中概率
- `lift_at(observation, k)`：AC@k 相对随机基线的倍数

去重是必须的：F-005 里 redis 占 rank 1-4，按候选条数算会把池子算成 5，
稀释服务级 AC@k。RCAEval 的 `main.py` 对服务级答案做同样去重。
"""
from __future__ import annotations

from dataclasses import dataclass

from evaluation.metrics import ScenarioOutcome


@dataclass(frozen=True)
class ChanceMetrics:
    """机会校正后的检索质量。"""

    candidate_pool_size: int
    chance_at_1: float
    chance_at_3: float
    chance_at_5: float
    lift_at_1: float | None
    lift_at_3: float | None
    lift_at_5: float | None

    def to_dict(self) -> dict:
        return {
            "candidate_pool_size": self.candidate_pool_size,
            "chance_at_1": self.chance_at_1,
            "chance_at_3": self.chance_at_3,
            "chance_at_5": self.chance_at_5,
            "lift_at_1": self.lift_at_1,
            "lift_at_3": self.lift_at_3,
            "lift_at_5": self.lift_at_5,
        }


def chance_at(pool_size: int, k: int) -> float:
    """随机从 pool 里取 k 个（不放回）至少命中一次的概率。

    pool_size <= 0 时返回 0（没有候选池就没有基线可言）。
    k >= pool_size 时返回 1.0：随机也能全覆盖，说明这个 k 无区分力。
    """
    if pool_size <= 0:
        return 0.0
    if k >= pool_size:
        return 1.0
    return round(k / pool_size, 4)


def lift_at(ac: float, chance: float) -> float | None:
    """Lift = AC / Chance。Chance 为 1（候选池比 k 还小）时无意义，返回 None。"""
    if chance >= 1.0:
        return None
    if chance <= 0:
        return None
    return round(ac / chance, 4)


def compute_chance_metrics(outcomes: list[ScenarioOutcome], k: int = 5) -> ChanceMetrics:
    """把一批场景结果聚合成机会校正指标。

    只统计"注入了故障"的场景（与 AC@k 的分母口径一致）。
    """
    fault_rows = [o for o in outcomes if o.fault_injected]
    pools = [o.candidate_pool_size for o in fault_rows if o.candidate_pool_size > 0]
    pool = max(pools) if pools else 0

    def _ac(attribute: str) -> float:
        values = [1.0 if getattr(o, attribute) else 0.0 for o in fault_rows]
        return round(sum(values) / len(values), 4) if values else 0.0

    ac1, ac3, ac5 = _ac("service_ac_at_1"), _ac("service_ac_at_3"), _ac("service_ac_at_5")
    return ChanceMetrics(
        candidate_pool_size=pool,
        chance_at_1=chance_at(pool, 1),
        chance_at_3=chance_at(pool, 3),
        chance_at_5=chance_at(pool, k),
        lift_at_1=lift_at(ac1, chance_at(pool, 1)),
        lift_at_3=lift_at(ac3, chance_at(pool, 3)),
        lift_at_5=lift_at(ac5, chance_at(pool, k)),
    )
