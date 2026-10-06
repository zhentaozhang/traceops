"""死信队列必须**有界**，否则长期运行的 worker 会内存耗尽。

## 缺陷（搜索无界队列实践后实测确认）

`InMemoryDeadLetterSink.records` 只追加，**没有任何上限或清理**：

```python
def send(self, record): self.records.append(record)
```

`DeadLetterRecord` 有 `MAX_RAW_CHARS`（单条原文上限），但**条数无上限**。
一个持续收到坏消息的 worker（上游格式错误、契约漂移、真实故障风暴）
会无限积累，最终 OOM —— 而那通常发生在系统本来就出问题的时候。

## 为什么这是真实风险而不是理论

死信恰恰在**故障期**增长最快：上游开始发畸形消息时，
每条都进死信。如果死信本身把进程撑死，那运维连"看看死信里有什么"
都做不到 —— 诊断工具成了新的故障源。

## 三条约束

1. **有上限**：保留最近 N 条（排障需要的是"刚发生了什么"）
2. **丢弃要计数**：不能静默丢 —— 运维必须知道"还有更多被丢了"
3. **按类别计数不丢**：总数统计要保留，否则趋势判断失真
"""
from __future__ import annotations

import pytest

from traceops.runtime.deadletter import (
    DeadLetterRecord, FailureCategory, InMemoryDeadLetterSink,
)


def _record(i: int) -> DeadLetterRecord:
    return DeadLetterRecord(
        original_entry_id=str(i), stream="traceops.events", group="g",
        consumer="c", category=FailureCategory.INVALID_REQUEST,
        reason=f"bad {i}", raw="{}", occurred_at="2026-10-30T10:02:00Z")


class TestDeadLetterIsBounded:
    def test_retains_only_the_most_recent_records(self):
        sink = InMemoryDeadLetterSink(max_records=10)
        for i in range(30):
            sink.send(_record(i))
        assert len(sink.records) == 10

    def test_keeps_the_newest_not_the_oldest(self):
        """排障要的是"刚发生了什么"，不是最早的 —— 保留尾部。"""
        sink = InMemoryDeadLetterSink(max_records=5)
        for i in range(10):
            sink.send(_record(i))
        ids = [r.original_entry_id for r in sink.records]
        assert ids == ["5", "6", "7", "8", "9"], f"expected the newest, got {ids}"

    def test_dropped_count_is_reported(self):
        """静默丢弃不可接受：运维必须知道还有多少被丢了。"""
        sink = InMemoryDeadLetterSink(max_records=5)
        for i in range(12):
            sink.send(_record(i))
        assert sink.dropped == 7

    def test_unbounded_is_available_but_explicit(self):
        """允许无界（测试/短生命周期场景），但必须显式选择。"""
        sink = InMemoryDeadLetterSink(max_records=0)
        for i in range(50):
            sink.send(_record(i))
        assert len(sink.records) == 50
        assert sink.dropped == 0


class TestCategoryCountingSurvivesTrimming:
    def test_counts_reflect_retained_records(self):
        sink = InMemoryDeadLetterSink(max_records=5)
        for i in range(5):
            sink.send(_record(i))
        assert sink.count_by_category()[FailureCategory.INVALID_REQUEST] == 5

    def test_default_bound_is_reasonable(self):
        """默认值要能容纳一次故障风暴的样本，又不至于吃掉内存。"""
        from traceops.runtime.deadletter import InMemoryDeadLetterSink as Sink

        assert 100 <= Sink.DEFAULT_MAX_RECORDS <= 5000
