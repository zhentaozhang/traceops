"""Avg@k 与 MRR：补偿 AC@k 的两个已知盲点。

## 为什么需要（借鉴 RCAEval / TriageRCA 的口径）

**盲点 1：AC@k 只看"有没有进前 k"，不看在第几位。**

```
案例 A: 根因在 rank 1    AC@5 = 1
案例 B: 根因在 rank 5    AC@5 = 1
```

两者 AC@5 完全相同，但诊断价值差别极大。`Avg@k = sum(AC@1..AC@k)/k`
（RCAEval `benchmark/evaluation.py:average`）对排名位置敏感：
A 的 Avg@5 = 1.0，B 的 Avg@5 = 0.2。

**盲点 2：完全没命中时 AC@k 全是 0，看不出"差多少"。**

`MRR = mean(1/rank_of_answer)`（TriageRCA 的 per_case schema 有这一列）
在命中时给 1/rank，未命中算 0 —— 它把"排第 2"和"排第 5"区分开。

这两个指标都能从已有数据算出来，不需要新采集。
"""
from __future__ import annotations

import pytest

from evaluation.rank_quality import average_precision_at, mean_reciprocal_rank, reciprocal_rank


class TestReciprocalRank:
    def test_rank_one_gives_one(self):
        assert reciprocal_rank(1) == 1.0

    def test_rank_three_gives_one_third(self):
        assert reciprocal_rank(3) == pytest.approx(1 / 3, rel=1e-6)

    def test_miss_gives_zero(self):
        """没找到就是 0 —— 不能给"接近"的安慰分。"""
        assert reciprocal_rank(None) == 0.0

    def test_zero_rank_is_treated_as_miss(self):
        assert reciprocal_rank(0) == 0.0


class TestMeanReciprocalRank:
    def test_single_case(self):
        assert mean_reciprocal_rank([1]) == 1.0

    def test_mixed_cases(self):
        # ranks 1, 2, miss -> (1 + 0.5 + 0)/3
        assert mean_reciprocal_rank([1, 2, None]) == pytest.approx(0.5, rel=1e-6)

    def test_all_missed_is_zero(self):
        assert mean_reciprocal_rank([None, None]) == 0.0

    def test_empty_input_is_zero(self):
        assert mean_reciprocal_rank([]) == 0.0


class TestAveragePrecisionAt:
    """Avg@k = sum(AC@1..AC@k)/k，对排名位置敏感。"""

    def test_perfect_ranking_scores_one(self):
        assert average_precision_at([1, 1, 1], k=3) == 1.0

    def test_worst_case_within_k_scores_one_over_k(self):
        """所有案例都刚好排在第 k 位 → Avg@k = 1/k（位置敏感性）。"""
        assert average_precision_at([3, 3, 3], k=3) == pytest.approx(1 / 3, abs=1e-4)

    def test_mixed(self):
        # ranks 1 and 2 at k=2: AC@1 = 1/2, AC@2 = 2/2 -> avg = 0.75
        assert average_precision_at([1, 2], k=2) == pytest.approx(0.75, rel=1e-6)

    def test_miss_beyond_k_contributes_zero(self):
        assert average_precision_at([None, None], k=2) == 0.0

    def test_k_zero_is_rejected(self):
        with pytest.raises(ValueError):
            average_precision_at([1], k=0)
