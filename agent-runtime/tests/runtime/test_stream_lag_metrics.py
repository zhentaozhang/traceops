"""消费者积压必须**可观测**，否则运维无法判断"消费是否跟得上"。

## 缺陷（搜索 Redis Streams 监控实践后确认）

`XPENDING` 与 `XINFO GROUPS` 是查看消费者组积压的标准手段，
而本项目的 Worker **完全没有实现它们**（grep `xpending|xinfo` 计数为 0）。

后果具体：当生产速率超过消费速率时

- 流长度上涨（`XLEN`），但**分不清是"刚写入"还是"没人消费"**
- PEL 里堆积着"已投递未 ACK"的消息，若 worker 崩溃则永远停在那里
- 运维看到的唯一信号是 Redis 内存缓慢上涨 —— 而这最容易被忽略

没有 lag 指标时，一个静默追赶不上的消费者和一个健康的空转消费者
**在外部完全无法区分**。

## 本模块提供

- `lag`：组内尚未投递的消息数（stream 长度 − 已投递数）
- `pending`：已投递但未 ACK 的数量（崩溃残留的直接证据）
- `consumers`：有几个消费者在跑（判断"是不是 worker 全挂了"）

## 边界

- **只读**：不改变消费行为，不动游标
- **不引入指标库**：返回普通 dict，接入 Prometheus 是部署层的事
"""
from __future__ import annotations

import pytest

from traceops.runtime.redis_source import RedisStreamEventSource


class FakeTransport:
    def __init__(self, *, groups=None, pending=None) -> None:
        self._groups = groups or []
        self._pending = pending
        self.commands: list[tuple] = []

    def xgroup_create(self, stream, group, *, start_id="0", mkstream=True):
        return None

    def xreadgroup(self, stream, group, consumer, count, block_ms, start_id=">"):
        return []

    def xautoclaim(self, stream, group, consumer, min_idle_ms, start_id, count):
        return []

    def xack(self, stream, group, entry_id):
        return 1

    def xinfo_groups(self, stream):
        """真实传输层会把 RESP 键值对数组转成 dict；这里模拟同一层。"""
        self.commands.append(("XINFO", "GROUPS", stream))
        out = []
        for entry in self._groups:
            if not isinstance(entry, (list, tuple)):
                out.append(entry)
                continue
            out.append({str(entry[i]): entry[i + 1]
                        for i in range(0, len(entry) - 1, 2)})
        return out

    def _command(self, *args):
        self.commands.append(args)
        name = str(args[0]).upper()
        if name == "XPENDING":
            return self._pending or [0]
        if name == "XPENDING":
            return self._pending
        if name == "XLEN":
            return 42
        return None


class TestLagMetricsShape:
    def test_lag_report_has_the_three_essential_fields(self):
        source = RedisStreamEventSource(
            FakeTransport(groups=[["name", "g", "consumers", 1, "pending", 2,
                                   "lag", 7]]),
            stream="s", group="g", consumer="c")
        report = source.lag_report()
        assert set(report) >= {"lag", "pending", "consumers"}, report

    def test_lag_is_reported_from_xinfo(self):
        source = RedisStreamEventSource(
            FakeTransport(groups=[["name", "g", "consumers", 1, "pending", 2,
                                   "lag", 7]]),
            stream="s", group="g", consumer="c")
        report = source.lag_report()
        assert report["lag"] == 7
        assert report["pending"] == 2

    def test_missing_group_reports_no_data_rather_than_zero(self):
        """组不存在时不能报 0 —— 那会伪装成"完全跟上了"。"""
        source = RedisStreamEventSource(
            FakeTransport(groups=[]), stream="s", group="absent", consumer="c")
        report = source.lag_report()
        assert report["lag"] is None, (
            f"an absent group must report None, not a fake 0: {report}")

    def test_report_is_read_only(self):
        """取指标不能改变消费状态（不能触发读、认领或建组）。"""
        transport = FakeTransport(groups=[{"name": "g", "lag": 0, "pending": 0,
                                           "consumers": 1}])
        source = RedisStreamEventSource(transport, stream="s", group="g", consumer="c")
        source.lag_report()
        names = [str(c[0]).upper() for c in transport.commands]
        assert "XREADGROUP" not in names
        assert "XGROUP" not in names
        assert "XAUTOCLAIM" not in names
