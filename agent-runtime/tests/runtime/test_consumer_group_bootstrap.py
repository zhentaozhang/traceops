"""消费者组必须能被**自动创建**，否则新环境永远无法消费。

## 缺陷（跨语言端到端实测发现）

Java 侧只做 `XADD traceops.events`（生产者不需要建组，这是 Redis Streams 的
正常分工）。而 Python 侧直接 `XREADGROUP ... GROUP traceops-workers`：

```
RedisCommandError: NOGROUP No such key 'traceops.events'
                   or consumer group 'traceops-workers'
```

**这不是配置问题，是缺少引导步骤。** 后果：任何全新部署（新的 Redis、
或被 FLUSH 过的实例）里 worker 立刻失败，且报错发生在读取阶段，
看起来像"Redis 有问题"而不是"少了一步初始化"。

## 正确行为

- 读取前确保组存在：`XGROUP CREATE <stream> <group> <start> MKSTREAM`
- `BUSYGROUP` 表示已存在 → **必须吞掉**（并发启动多个 worker 时必然出现）
- 起始位置用 `0` 而不是 `$`：`$` 会跳过**已有**的积压消息，
  那正是首次部署时最需要处理的那些

## 为什么起始位置决定正确性

`$` = 从"现在"开始，会静默丢弃已经入队的事件 ——
运维看到的症状是"事故建了但永远没结论"，而日志里一切正常。
`0` = 从头读，积压会被处理；幂等由 run 账本保证，重复消费不会重复调查。
"""
from __future__ import annotations

import pytest

from traceops.runtime.redis_source import RedisStreamEventSource


class FakeTransport:
    """记录命令，按脚本返回。"""

    def __init__(self, replies: dict[str, object] | None = None) -> None:
        self.commands: list[tuple] = []
        self.replies = replies or {}

    def xreadgroup(self, stream, group, consumer, count, block_ms, start_id=">"):
        self.commands.append(("XREADGROUP", stream, group, consumer))
        return []
        self.commands.append(("XREADGROUP", group, consumer, stream))
        return []

    def xack(self, stream, group, entry_id):
        return 1

    def xautoclaim(self, stream, group, consumer, min_idle_ms, start_id, count):
        self.commands.append(("XAUTOCLAIM", stream, group, consumer))
        return []

    def xgroup_create(self, stream, group, *, start_id="0", mkstream=True):
        self.commands.append(("XGROUP", "CREATE", stream, group, start_id, "MKSTREAM"))
        if "XGROUP" in self.replies:
            value = self.replies["XGROUP"]
            if isinstance(value, Exception):
                raise value
            return value
        return None


class TestGroupIsCreatedBeforeReading:
    def test_group_creation_is_attempted(self):
        transport = FakeTransport()
        source = RedisStreamEventSource(
            transport, stream="traceops.events", group="traceops-workers",
            consumer="w1")
        source.poll()

        calls = [c[0].upper() for c in transport.commands]
        assert "XGROUP" in calls, (
            f"a consumer group must be created before reading; got {calls}")

    def test_creation_uses_mkstream_and_starts_from_zero(self):
        """MKSTREAM：流还不存在时也要能建组；起点 0：不跳过积压。"""
        transport = FakeTransport()
        source = RedisStreamEventSource(
            transport, stream="s", group="g", consumer="c")
        source.poll()

        xgroup = next(c for c in transport.commands if str(c[0]).upper() == "XGROUP")
        assert "MKSTREAM" in [str(a).upper() for a in xgroup]
        # 起始位置必须是 0（而不是 $），否则首次部署会丢掉已积压的事件
        assert "0" in [str(a) for a in xgroup], f"got {xgroup}"

    def test_busygroup_is_tolerated(self):
        """组已存在是**正常**情况（并发启动/重启），不能让它中断读取。"""
        transport = FakeTransport(replies={
            "XGROUP": Exception("BUSYGROUP Consumer Group name already exists")})
        source = RedisStreamEventSource(
            transport, stream="s", group="g", consumer="c")
        # 不应抛出
        source.poll()

    def test_creation_happens_once_only(self):
        """每轮 read 都建组会浪费往返；缓存"已建"状态是必要的。"""
        transport = FakeTransport()
        source = RedisStreamEventSource(
            transport, stream="s", group="g", consumer="c")
        source.poll()
        source.poll()

        xgroup_calls = [c for c in transport.commands if str(c[0]).upper() == "XGROUP"]
        assert len(xgroup_calls) == 1, f"expected 1 XGROUP, got {len(xgroup_calls)}"
