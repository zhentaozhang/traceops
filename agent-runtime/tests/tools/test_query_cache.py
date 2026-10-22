"""查询缓存：省掉真正的重复查询，但绝不冒充"刚取到的"。

## 为什么需要

告警风暴会产生**同一服务、同一时间窗**的多条事故（它们本来就是同一次故障
的不同切面）。每条事故都要跑一遍 12 条查询 —— 参数完全相同。
没有缓存时这是纯粹重复的外网流量。

## 两条必须守住的线

1. **键按内容寻址**：参数顺序不影响命中，参数值变了必须重新取。
   用 `repr` 做键会随字典插入顺序变化，于是"同一份参数"被当成两条查询 ——
   缓存看起来在工作，实际几乎不命中。
2. **命中要带原始取数时刻**：证据有 `observed_at`（数据描述哪一刻）与
   `collected_at`（我们什么时候拿到它）。从缓存来的数据，后者必须是
   **当初**那次取数的时刻，否则报告会声称"这份日志是刚抓的"。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.tools.query_cache import CACHE_TTL, QueryCache

NOW = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)
PARAMS = {"service": "order-service", "start": "2026-10-30T10:00:00Z",
          "end": "2026-10-30T10:05:00Z"}


class TestKeys:
    def test_key_is_independent_of_dict_order(self):
        a = QueryCache.key("query_service_logs", {"service": "s", "start": "1", "end": "2"})
        b = QueryCache.key("query_service_logs", {"end": "2", "start": "1", "service": "s"})

        assert a == b

    def test_a_different_value_is_a_different_key(self):
        a = QueryCache.key("query_service_logs", {"service": "a"})
        b = QueryCache.key("query_service_logs", {"service": "b"})

        assert a != b

    def test_a_different_tool_is_a_different_key(self):
        assert QueryCache.key("query_service_logs", PARAMS) != QueryCache.key(
            "query_service_error_rate", PARAMS)


class TestRoundTrip:
    def test_a_stored_result_can_be_read_back(self):
        cache = QueryCache()
        cache.put("query_service_logs", PARAMS, {"data": 1}, at=NOW)

        # 必须显式给 `now`：默认取墙钟，而 `NOW` 是固定历史时刻 ——
        # 不传就等于用 2026-11-01 去查 2026-10-30 抓的数据，早已过期。
        cached = cache.get("query_service_logs", PARAMS, now=NOW)

        assert cached is not None
        assert cached.payload == {"data": 1}
        assert cached.fetched_at == NOW

    def test_the_note_carries_the_original_fetch_time(self):
        cache = QueryCache()
        cache.put("query_service_logs", PARAMS, {"data": 1}, at=NOW)

        note = cache.get("query_service_logs", PARAMS, now=NOW).note()

        assert note == {"tool": "query_service_logs", "fetched_at": NOW.isoformat()}

    def test_an_unknown_query_misses(self):
        assert QueryCache().get("query_service_logs", PARAMS) is None


class TestStaleness:
    def test_an_entry_older_than_the_ttl_is_not_served(self):
        cache = QueryCache()
        cache.put("query_service_logs", PARAMS, {"data": 1}, at=NOW)

        stale = cache.get("query_service_logs", PARAMS, now=NOW + CACHE_TTL + timedelta(seconds=1))

        # Prometheus 回填通常几分钟内完成：过了这个尺度，同一窗口的结果
        # 确实可能已经变了 —— 宁可多查一次，也不要把过期数据当成刚取的
        assert stale is None

    def test_a_fresh_entry_is_served(self):
        cache = QueryCache()
        cache.put("query_service_logs", PARAMS, {"data": 1}, at=NOW)

        assert cache.get("query_service_logs", PARAMS,
                         now=NOW + timedelta(seconds=30)) is not None

    def test_a_stale_entry_is_dropped_rather_than_kept_forever(self):
        cache = QueryCache()
        cache.put("query_service_logs", PARAMS, {"data": 1}, at=NOW)
        cache.get("query_service_logs", PARAMS, now=NOW + CACHE_TTL + timedelta(minutes=1))

        assert cache.stats()["size"] == 0


class TestBounds:
    def test_the_cache_does_not_grow_without_a_limit(self):
        cache = QueryCache(limit=2)
        for index in range(3):
            cache.put("query_service_logs", {"service": f"svc-{index}"}, {"n": index})

        assert cache.stats()["size"] == 2

    def test_the_oldest_entry_is_evicted_first(self):
        cache = QueryCache(limit=2)
        cache.put("query_service_logs", {"service": "a"}, {"n": 1})
        cache.put("query_service_logs", {"service": "b"}, {"n": 2})
        cache.put("query_service_logs", {"service": "c"}, {"n": 3})

        assert cache.get("query_service_logs", {"service": "a"}) is None
        assert cache.get("query_service_logs", {"service": "b"}) is not None

    def test_hits_and_misses_are_counted(self):
        cache = QueryCache()
        cache.put("query_service_logs", PARAMS, {"data": 1})
        cache.get("query_service_logs", PARAMS)
        cache.get("query_service_logs", {"service": "other"})

        stats = cache.stats()
        assert stats["hits"] == 1 and stats["misses"] == 1

    def test_invalid_limits_are_refused(self):
        with pytest.raises(ValueError):
            QueryCache(limit=0)
        with pytest.raises(ValueError):
            QueryCache(ttl=timedelta(0))
