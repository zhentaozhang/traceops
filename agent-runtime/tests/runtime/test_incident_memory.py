"""跨事故记忆：只记事实、只给先验、只能调权。

## 被替换的缺陷

一次调查只看得到自己那个窗口。"order-service 每月因连接池耗尽挂一次"
这件事，对每一次调查都是全新的：每次从零推导，每次结论都可能不同
（同样的证据，排序靠 UUID 兜底）。人靠经验一眼认出"又是它"，
系统不能 —— 因为经验从来没被记下来。

## 这个文件盯住的三条线

1. **只记结局**：命中记 hits、被推翻记 refuted。两个都记，
   否则记忆会变成"永远在说同一个名字"的机器。
2. **净值为正才算见过**：反复被推翻的候选，"它又来了"不该加分。
3. **有上限**：记忆是辅助信号，不值得无界增长。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.runtime.incident_memory import IncidentMemory, Prior

NOW = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _memory(**kwargs) -> IncidentMemory:
    return IncidentMemory(**kwargs)


class TestRecordingOutcomes:
    def test_a_hit_is_remembered(self):
        memory = _memory()

        prior = memory.record(service="order-service", fault_type="pool_exhaustion",
                              component="redis-connection-pool", at=NOW)

        assert isinstance(prior, Prior)
        assert prior.hits == 1 and prior.refuted == 0
        assert prior.corroborated is True

    def test_records_accumulate(self):
        memory = _memory()
        memory.record(service="order-service", fault_type="pool_exhaustion", at=NOW)
        prior = memory.record(service="order-service", fault_type="pool_exhaustion",
                              at=NOW + timedelta(days=30))

        assert prior.hits == 2
        assert prior.last_seen == NOW + timedelta(days=30)

    def test_a_refutation_is_remembered_too(self):
        memory = _memory()

        prior = memory.record(service="order-service", fault_type="pool_exhaustion",
                              refuted=True, at=NOW)

        assert prior.hits == 0 and prior.refuted == 1
        # 被推翻过的东西"又来了"不该加分 —— 否则会锁死在同一个错误答案上
        assert prior.corroborated is False

    def test_hits_and_refutations_net_out(self):
        memory = _memory()
        memory.record(service="svc", fault_type="latency_degradation", at=NOW)
        memory.record(service="svc", fault_type="latency_degradation", at=NOW)
        prior = memory.record(service="svc", fault_type="latency_degradation",
                              refuted=True, at=NOW)

        assert prior.net == 1 and prior.corroborated is True


class TestRecall:
    def test_unknown_service_is_unknown_not_zero(self):
        # None = "从没见过"；Prior(hits=0) = "见过但全被推翻"。
        # 混同会让第一次出现的服务看起来像"老熟人都被推翻了"。
        assert _memory().recall(service="never-seen", fault_type="x") is None

    def test_recall_without_a_component_aggregates_across_components(self):
        memory = _memory()
        memory.record(service="order-service", fault_type="pool_exhaustion",
                      component="redis-connection-pool", at=NOW)
        memory.record(service="order-service", fault_type="pool_exhaustion",
                      component="mysql-connection-pool", at=NOW)

        prior = memory.recall(service="order-service", fault_type="pool_exhaustion")

        assert prior is not None and prior.hits == 2

    def test_recall_with_a_component_is_exact(self):
        memory = _memory()
        memory.record(service="order-service", fault_type="pool_exhaustion",
                      component="redis-connection-pool", at=NOW)

        assert memory.recall(service="order-service", fault_type="pool_exhaustion",
                             component="mysql-connection-pool") is None

    def test_empty_inputs_recall_nothing(self):
        assert _memory().recall(service="", fault_type="x") is None


class TestBounds:
    def test_the_table_does_not_grow_without_a_limit(self):
        memory = _memory(limit=2)
        for index in range(3):
            memory.record(service=f"svc-{index}", fault_type="pool_exhaustion",
                          at=NOW + timedelta(minutes=index))

        summary = memory.summary(limit=10)

        assert len(summary) == 2
        # 最早那条被淘汰，最近两条还在
        assert [entry["service"] for entry in summary][-1] in {"svc-1", "svc-2"}
        assert "svc-0" not in {entry["service"] for entry in summary}

    def test_a_non_positive_limit_is_refused(self):
        with pytest.raises(ValueError):
            _memory(limit=0)

    def test_recording_without_a_pair_is_refused(self):
        with pytest.raises(ValueError):
            _memory().record(service="", fault_type="pool_exhaustion")

    def test_summary_is_ordered_by_net(self):
        memory = _memory()
        memory.record(service="often", fault_type="pool_exhaustion", at=NOW)
        memory.record(service="often", fault_type="pool_exhaustion", at=NOW)
        memory.record(service="once", fault_type="pool_exhaustion", at=NOW)

        assert [entry["service"] for entry in memory.summary()] == ["often", "once"]


class TestPersistence:
    def test_memory_survives_a_restart(self, tmp_path):
        path = tmp_path / "memory.sqlite"
        first = IncidentMemory(path=path)
        first.record(service="order-service", fault_type="pool_exhaustion", at=NOW)
        first.close()

        second = IncidentMemory(path=path)
        prior = second.recall(service="order-service", fault_type="pool_exhaustion")
        second.close()

        # 记忆的价值恰恰在于跨进程重启（"每月挂一次"必然跨越很多次重启）
        assert prior is not None and prior.hits == 1

    def test_forget_removes_a_row(self):
        memory = _memory()
        memory.record(service="svc", fault_type="pool_exhaustion", at=NOW)

        memory.forget(service="svc", fault_type="pool_exhaustion")

        assert memory.recall(service="svc", fault_type="pool_exhaustion") is None
