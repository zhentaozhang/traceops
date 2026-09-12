"""排名质量指标：Avg@k 与 MRR。

## 为什么 AC@k 不够

**AC@k 只看"有没有进前 k"，不看排在第几位。**

```
案例 A: 根因在 rank 1    AC@5 = 1
案例 B: 根因在 rank 5    AC@5 = 1
```

两者 AC@5 相同，但诊断价值差别极大 —— 排第一意味着直接命中，
排第五意味着值班同学还要自己再判断一轮。只报 AC@k 会把这种差别藏起来。

**Avg@k**（RCAEval `benchmark/evaluation.py` 的 `average`）：

```
Avg@k = sum(AC@1, AC@2, ..., AC@k) / k
```

对位置敏感：全排第一 → 1.0；全排第 k → 1/k。

**MRR**（Mean Reciprocal Rank，TriageRCA 的 per_case schema 有这一列）：

```
MRR = mean(1/rank_of_answer)，未命中计 0
```

把"排第 2"（0.5）和"排第 5"（0.2）区分开。

## 与 AC@k 的关系

三者都从同一份排名数据算出来，不需要新采集。区别是分辨率：
AC@k 是阶跃函数，Avg@k 与 MRR 是连续的。
"""
from __future__ import annotations


def reciprocal_rank(rank: int | None) -> float:
    """单案例的倒数排名。rank 为 None 或 <=0 表示未命中 → 0.0。

    未命中给 0 而不是"接近"的小数：那会让"完全没找到"看起来像"差不多找到了"。
    """
    if rank is None or rank <= 0:
        return 0.0
    return 1.0 / rank


def mean_reciprocal_rank(ranks: list[int | None]) -> float:
    """一批案例的 MRR。空集合返回 0.0。"""
    if not ranks:
        return 0.0
    return round(sum(reciprocal_rank(r) for r in ranks) / len(ranks), 4)


def average_precision_at(ranks: list[int | None], k: int) -> float:
    """Avg@k = mean over cases of (sum(AC@1..AC@k) / k)。

    逐案例算法等价于先算每个案例的 AC@j 再平均 —— 这样单案例的"排第几"
    不会因为别的案例而稀释。
    """
    if k < 1:
        raise ValueError(f"k must be >= 1 (got {k})")
    if not ranks:
        return 0.0
    per_case: list[float] = []
    for rank in ranks:
        # 该案例在 1..k 各档的 AC
        hits = sum(1 for j in range(1, k + 1) if rank is not None and rank <= j)
        per_case.append(hits / k)
    return round(sum(per_case) / len(per_case), 4)
