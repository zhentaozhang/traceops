"""查询结果缓存：同一趟调查里同一条查询不重复打数据源。

## 为什么值得做

判别回路（`graph._discrimination_items`）会把首轮的查询在**更窄的窗口**里
再问一遍 —— 这当然不是同一条查询，参数不同，不命中缓存，是对的。
但有些重复是真的重复：同一服务、同一窗口、同一工具，在不同阶段被问两次
（拓扑里两个候选共享同一个下游、`profile` 与错误率落在同一服务上、
模型假设补充阶段又查一遍）。每一次都打一次外网。

## 关键的诚实性要求

缓存命中**不能**让证据看起来像刚取到的。证据有两个时间：

  - `observed_at`：数据描述的那个时刻（来自数据本身，与取数方式无关）；
  - `collected_at`：我们**真正把它取回来**的时刻。

从缓存里拿到的数据，`collected_at` 必须是**当初那次取数**的时刻。
否则报告会说"这份日志是 10:05 抓的"，而实际是 09:50 抓的 ——
复盘时"为什么当时没看到变化"会被这个时间戳骗过去。

所以缓存返回的不是裸 payload，而是带 `fetched_at` 的记录，
由 `_collect_node` 传给 `normalize(collected_at=...)`。

## 为什么有上限

一条调查里查询条数受预算约束（默认 40 次），本来就不会爆。
但缓存也可能被跨调查复用（同一个 worker 顺序处理多条事故），
无上限就是把"省流量"换成"吃内存"。超出上限淘汰最久没用过的那条。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

CACHE_LIMIT = 128
"""保留的查询结果条数上限。"""

CACHE_TTL = timedelta(minutes=5)
"""命中多久之内的结果才算"同一份数据"。

没有 TTL 的缓存会给"同一窗口、晚到数据"制造假象：Prometheus 回填通常
在几分钟内完成，超过这个尺度同一窗口的查询结果确实可能已经变了。
宁可多查一次，也不要把过期数据当成刚取的。"""


@dataclass(frozen=True)
class CachedResult:
    """一次被缓存下来的取数结果。"""

    tool_name: str
    key: str
    payload: Any
    fetched_at: datetime

    def note(self) -> dict[str, Any]:
        return {
            "tool": self.tool_name,
            "fetched_at": self.fetched_at.isoformat(),
        }


class QueryCache:
    """按 (工具, 参数) 内容寻址的结果缓存。"""

    def __init__(self, *, limit: int = CACHE_LIMIT,
                 ttl: timedelta = CACHE_TTL) -> None:
        if limit < 1:
            raise ValueError(f"cache limit must be positive, got {limit}")
        if ttl <= timedelta(0):
            raise ValueError(f"cache ttl must be positive, got {ttl}")
        self._limit = limit
        self._ttl = ttl
        self._entries: dict[str, CachedResult] = {}
        self._hits = 0
        self._misses = 0

    # ---------- 键 ----------

    @staticmethod
    def key(tool_name: str, params: dict[str, Any]) -> str:
        """内容寻址：参数顺序不影响命中，参数值变了必须重新取。

        用规范 JSON（`sort_keys`）而不是 `repr`：`repr` 会随字典插入顺序变化，
        于是"同一份参数"被当成两条查询 —— 缓存看起来在工作，实际几乎不命中。
        """
        canonical = json.dumps(
            {"tool": tool_name, "params": params},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ---------- 读写 ----------

    def get(self, tool_name: str, params: dict[str, Any],
            *, now: datetime | None = None) -> CachedResult | None:
        key = self.key(tool_name, params)
        cached = self._entries.get(key)
        if cached is None:
            self._misses += 1
            return None
        moment = now or datetime.now(UTC)
        if moment - cached.fetched_at > self._ttl:
            # 过期的条目直接扔掉：留着它只会在下次查询时再多一次时间比较
            self._entries.pop(key, None)
            self._misses += 1
            return None
        # 命中即刷新 LRU 位置（dict 保序）
        self._entries.pop(key)
        self._entries[key] = cached
        self._hits += 1
        return cached

    def put(self, tool_name: str, params: dict[str, Any], payload: Any,
            *, at: datetime | None = None) -> CachedResult:
        key = self.key(tool_name, params)
        cached = CachedResult(
            tool_name=tool_name,
            key=key,
            payload=payload,
            fetched_at=at or datetime.now(UTC),
        )
        self._entries.pop(key, None)
        self._entries[key] = cached
        self._evict()
        return cached

    def clear(self) -> None:
        self._entries.clear()

    def stats(self) -> dict[str, int]:
        return {
            "size": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
            "limit": self._limit,
            "ttl_seconds": int(self._ttl.total_seconds()),
        }

    def _evict(self) -> None:
        overflow = len(self._entries) - self._limit
        for key in list(self._entries)[:overflow]:
            self._entries.pop(key, None)
