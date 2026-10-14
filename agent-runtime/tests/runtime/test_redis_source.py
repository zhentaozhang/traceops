"""Redis Streams 事件源测试：消费端的契约（而不是"能跑起来"）。

为什么这些用例值得写：

- `InvestigationWorker` 用 `message["id"]` 做 ACK —— 只要事件源不注入 id，
  整条闭环就是"消息收下了但永远不确认"，重启后 pending 堆积、无限重投。
  所以 id 是**协议的一部分**，必须单独用测试钉住。
- Redis Streams 的崩溃恢复能力只在 XAUTOCLAIM 里：上次进程被杀时
  未 ACK 的消息必须**优先**回到消费列表最前面，否则恢复顺序退化成
  "新消息先跑、崩溃前那批最后才跑"，调查结果会乱序。
- 坏消息不能静默丢：上游 schema 出问题时，运维要能审计到原文，
  而不是对着一个空批次猜"是不是 Redis 没数据"。

`ScriptedTransport` 是测试用的可替换传输层：它按脚本返回
`xreadgroup` / `xautoclaim` 的结果，并记录每次调用的参数。
生产环境的传输层实现见 `redis_source.py` 里的桩与说明。
"""
from __future__ import annotations

import json

import pytest

from traceops.runtime.redis_source import (
    DEFAULT_RECLAIM_START_ID,
    PAYLOAD_FIELD,
    RedisStreamEventSource,
    UnimplementedRedisTransport,
    WorkerSourceAdapter,
)
from traceops.runtime.worker import (
    InMemoryEventSink,
    InvestigationWorker,
    derive_request_from_event,
)

STREAM = "traceops.events"
GROUP = "traceops-agent-runtime"
CONSUMER = "worker-1"


def requested_event(run_id: str = "run-001", incident_id: str = "inc-001") -> dict:
    """构造一条符合 contracts/ 两层结构的事件（envelope + payload）。"""
    return {
        "envelope": {
            "event_id": "evt-11111111-2222-3333-4444-555555555555",
            "event_type": "investigation.requested",
            "schema_version": "1.0",
            "occurred_at": "2026-10-30T10:06:00Z",
            "correlation_id": incident_id,
            "causation_id": None,
            "aggregate_id": run_id,
            "aggregate_version": 1,
            "producer": "control-plane",
        },
        "payload": {
            "investigation_run_id": run_id,
            "incident_id": incident_id,
            "environment": "demo",
            "service_hint": "order-service",
            "time_range": {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:06:00Z"},
            "config_version": "cfg",
            "priority": "high",
        },
    }


def stream_fields(event: dict | str, **extra: str) -> dict[str, str]:
    """把事件编码成 Streams entry 的 field map。

    Java 端 `RedisStreamEventPublisher` 只写一个 `payload` 字段，
    这里保留同样的主字段，并允许测试塞入额外的无关字段。
    """
    body = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
    return {PAYLOAD_FIELD: body, **extra}


class ScriptedTransport:
    """测试用传输层：脚本化返回 + 记录调用参数。

    排队语义：
    - `queue_claim(...)` 依次消费 `xautoclaim` 的返回，排空后返回空列表
    - `queue_read(...)` 依次消费 `xreadgroup` 的返回，排空后返回空列表
    这样"第二次 poll 拿到什么"是显式的，测试不需要依赖实现内部状态。
    """

    def __init__(self) -> None:
        self.claim_batches: list[list[tuple[str, dict[str, str]]]] = []
        self.read_batches: list[list[tuple[str, list[tuple[str, dict[str, str]]]]]] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.acked: list[tuple[str, str, str]] = []
        self._known_ids: set[str] = set()

    # ---- 脚本 ----

    def queue_claim(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        self.claim_batches.append(entries)
        self._known_ids.update(entry_id for entry_id, _ in entries)

    def queue_read(self, streams: list[tuple[str, list[tuple[str, dict[str, str]]]]]) -> None:
        self.read_batches.append(streams)
        for _, entries in streams:
            self._known_ids.update(entry_id for entry_id, _ in entries)

    # ---- RedisCommandTransport ----

    def xreadgroup(self, group, consumer, stream, count, block_ms):
        self.calls.append(("xreadgroup", {"group": group, "consumer": consumer,
                                          "stream": stream, "count": count, "block_ms": block_ms}))
        return self.read_batches.pop(0) if self.read_batches else []

    def xgroup_create(self, stream, group, *, start_id="0", mkstream=True):
        """消费者组引导：真实传输层会发 XGROUP CREATE；替身只需记录。"""
        self.calls.append(("xgroup_create", {"stream": stream, "group": group,
                                             "start_id": start_id, "mkstream": mkstream}))

    def xack(self, stream, group, entry_id):
        self.calls.append(("xack", {"stream": stream, "group": group, "entry_id": entry_id}))
        self.acked.append((stream, group, entry_id))
        # 只有真实存在于 Streams 的条目才返回 1，模拟 Redis 的语义
        return 1 if entry_id in self._known_ids else 0

    def xautoclaim(self, stream, group, consumer, min_idle_ms, start_id, count):
        self.calls.append(("xautoclaim", {"stream": stream, "group": group, "consumer": consumer,
                                          "min_idle_ms": min_idle_ms, "start_id": start_id, "count": count}))
        return self.claim_batches.pop(0) if self.claim_batches else []


def make_source(transport: ScriptedTransport | None = None, *, block_ms: int = 1000,
                min_idle_ms: int = 60_000) -> tuple[RedisStreamEventSource, ScriptedTransport]:
    transport = transport or ScriptedTransport()
    source = RedisStreamEventSource(transport, stream=STREAM, group=GROUP, consumer=CONSUMER,
                                   block_ms=block_ms, min_idle_ms=min_idle_ms)
    return source, transport


class TestPollContract:
    def test_every_polled_message_carries_id_key(self):
        """ACK 只能靠 id：任何一条消息缺 id，worker 的 ack 路径就断。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [("1700000000000-0", stream_fields(requested_event()))])])

        messages = source.poll(limit=10)

        assert len(messages) == 1
        assert messages[0]["id"] == "1700000000000-0"
        # 形状是 {id, payload}：整份事件文档（含 envelope）放在 payload 里，
        # 与 worker.py 的 InMemoryEventSource 摊平形状不同，见 WorkerSourceAdapter
        assert set(messages[0]) == {"id", "payload"}

    def test_payload_is_json_decoded_not_raw_string(self):
        """上层按 dict 取字段（worker 的 _extract_run_id 就是这么做的）。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [("1700000000000-0", stream_fields(requested_event()))])])

        message = source.poll(limit=10)[0]

        assert isinstance(message["payload"], dict), "payload 必须是解析后的对象"
        assert message["payload"]["envelope"]["event_type"] == "investigation.requested"
        assert message["payload"]["payload"]["investigation_run_id"] == "run-001"

    def test_reclaimed_pending_messages_come_first(self):
        """崩溃恢复优先：崩溃前那批未 ACK 的消息必须排在新消息之前。"""
        source, transport = make_source()
        pending = ("1700000000000-0", stream_fields(requested_event(run_id="run-pending")))
        cached = ("1700000000005-0", stream_fields(requested_event(run_id="run-new")))
        transport.queue_claim([pending])
        transport.queue_read([(STREAM, [cached])])

        messages = source.poll(limit=10)

        assert [m["id"] for m in messages] == ["1700000000000-0", "1700000000005-0"]
        assert messages[0]["payload"]["payload"]["investigation_run_id"] == "run-pending"
        # 认领先于读取，且新消息的 count 扣掉已认领数量
        # 现在会先确保消费者组存在（全新部署必需），再认领+读取。
        # 只断言**读取阶段**的相对顺序，不钉死引导步骤的绝对位置。
        read_phase = [name for name, _ in transport.calls
                      if name in ("xautoclaim", "xreadgroup")]
        assert read_phase == ["xautoclaim", "xreadgroup"]
        claim = next(p for n, p in transport.calls if n == "xautoclaim")
        assert claim["start_id"] == DEFAULT_RECLAIM_START_ID
        assert claim["min_idle_ms"] == 60_000
        read = next(p for n, p in transport.calls if n == "xreadgroup")
        assert read["count"] == 9

    def test_empty_autoclaim_does_not_block_new_reads(self):
        """没有 pending 不是异常路径，正常读新消息即可。"""
        source, transport = make_source()
        transport.queue_claim([])
        transport.queue_read([(STREAM, [("1700000000000-1", stream_fields(requested_event()))])])

        messages = source.poll(limit=10)

        assert len(messages) == 1
        assert messages[0]["id"] == "1700000000000-1"
        names = [n for n, _ in transport.calls]
        assert "xautoclaim" in names and "xreadgroup" in names
        # 认领必须排在读取之前（崩溃恢复优先）
        assert names.index("xautoclaim") < names.index("xreadgroup")

    def test_empty_stream_returns_empty_list(self):
        """空批次必须是空 list（不是 None），调用方会直接迭代。"""
        source, transport = make_source()

        messages = source.poll(limit=10)

        assert messages == []
        assert isinstance(messages, list)
        # 试探性读取也要走完两条命令，否则无法区分"没消息"和"没调用"
        # 现在会先确保消费者组存在（全新部署必需），再认领+读取。
        # 只断言**读取阶段**的相对顺序，不钉死引导步骤的绝对位置。
        read_phase = [name for name, _ in transport.calls
                      if name in ("xautoclaim", "xreadgroup")]
        assert read_phase == ["xautoclaim", "xreadgroup"]

    def test_extra_stream_fields_do_not_break_parsing(self):
        """entry 上除 payload 外的字段（如 ts / producer）是允许的。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [("1700000000000-0",
                                          stream_fields(requested_event(), ts="1769783160000"))])])

        message = source.poll(limit=10)[0]

        assert message["id"] == "1700000000000-0"
        assert message["payload"]["envelope"]["event_type"] == "investigation.requested"
        # 无关字段不污染消息体：只有 payload 被展开，ts 不该出现在顶层
        assert "ts" not in message

    def test_non_detail_extra_field_is_ignored_without_losing_id(self):
        """补充：payload 缺失时 field 里其他内容仍要能审计（见 malformed 用例）。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [("1700000000000-0", {"ts": "1769783160000"})])])

        message = source.poll(limit=10)[0]

        assert message["id"] == "1700000000000-0"
        assert message["malformed"] is True


class TestMalformedMessages:
    def test_bad_json_is_kept_and_marked_malformed(self):
        """坏 JSON 不能静默丢弃：留下来带原文，才能审计上游 schema 事故。"""
        source, transport = make_source()
        bad_payload = '{"envelope": {"event_type": "investigation.requested"}, "payload": '
        transport.queue_read([(STREAM, [("1700000000000-0", stream_fields(bad_payload))])])

        messages = source.poll(limit=10)

        assert len(messages) == 1, "坏消息不能被吞掉"
        malformed = messages[0]
        assert malformed["id"] == "1700000000000-0"
        assert malformed["malformed"] is True
        assert malformed["raw"] == bad_payload
        # 不能伪装成正常事件：没有解析出来的 envelope / payload
        assert "envelope" not in malformed
        assert "payload" not in malformed

    def test_valid_message_is_not_marked_malformed(self):
        """反向断言：malformed 标记必须来自解析失败，不是无条件加的。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [("1700000000000-0", stream_fields(requested_event()))])])

        message = source.poll(limit=10)[0]

        assert "malformed" not in message
        assert "raw" not in message


class TestAck:
    def test_ack_uses_configured_stream_and_group(self):
        """ACK 需要 (stream, group, entry_id)：类必须记住 stream/group 配置。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [("1700000000000-0", stream_fields(requested_event()))])])
        message = source.poll(limit=10)[0]

        source.ack(message["id"])

        assert transport.acked == [(STREAM, GROUP, "1700000000000-0")]
        assert transport.calls[-1] == ("xack", {"stream": STREAM, "group": GROUP,
                                                "entry_id": "1700000000000-0"})

    def test_ack_of_malformed_message_still_uses_its_id(self):
        """坏消息也要能 ACK 掉，否则它会永远卡在 PEL 里被反复认领。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [("1700000000000-0", stream_fields("{not json"))])])
        message = source.poll(limit=10)[0]

        source.ack(message["id"])

        assert transport.acked == [(STREAM, GROUP, "1700000000000-0")]


class TestConstructionValidation:
    @pytest.mark.parametrize("field,value", [
        ("stream", ""),
        ("stream", "   "),
        ("group", ""),
        ("consumer", ""),
        ("consumer", " \t\n"),
    ])
    def test_blank_stream_group_consumer_raise_value_error(self, field, value):
        """空配置会让 XREADGROUP/ACK 打到一个不存在的消费者组 —— 启动即失败更好。"""
        kwargs = {"stream": STREAM, "group": GROUP, "consumer": CONSUMER, field: value}
        with pytest.raises(ValueError, match=field):
            RedisStreamEventSource(ScriptedTransport(), **kwargs)

    def test_negative_block_ms_raises_value_error(self):
        with pytest.raises(ValueError, match="block_ms"):
            RedisStreamEventSource(ScriptedTransport(), stream=STREAM, group=GROUP,
                                   consumer=CONSUMER, block_ms=-1)


class TestDuplicateEntryIds:
    def test_same_entry_id_across_polls_is_stable_and_harmless(self):
        """XAUTOCLAIM 会重复交回未 ACK 的消息：重复投递由 worker 按 run_id 去重，
        事件源不做隐藏状态去重（隐藏去重会掩盖 ACK 丢失）。"""
        source, transport = make_source()
        fields = stream_fields(requested_event())
        transport.queue_claim([("1700000000000-0", fields)])
        transport.queue_read([(STREAM, [])])
        transport.queue_claim([("1700000000000-0", fields)])
        transport.queue_read([(STREAM, [])])

        first = source.poll(limit=10)
        second = source.poll(limit=10)

        assert [m["id"] for m in first] == ["1700000000000-0"]
        assert [m["id"] for m in second] == ["1700000000000-0"]
        assert first[0]["payload"] == second[0]["payload"]
        # 两次 poll 都返回了同一条：行为可预测，且不 crash
        assert transport.acked == []


class TestWorkerProtocolCompatibility:
    def test_worker_polls_and_acks_through_the_same_protocol(self):
        """真实闭环证据：InvestigationWorker 不认 transport，只认 EventSource。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [
            ("1700000000000-1", stream_fields(requested_event(run_id="run-001"))),
            ("1700000000000-2", stream_fields("{broken")),
        ])])

        def derive_request(message):
            # 这里只验证协议（poll 给 id、worker 用 id ACK），不跑真调查
            raise ValueError("protocol verification only")

        def build_context(request):
            raise AssertionError("must not be reached when derive_request raises")

        sink = InMemoryEventSink()
        worker = InvestigationWorker(source=source, sink=sink, derive_request=derive_request,
                                     build_context=build_context)

        stats = worker.poll_once(limit=10)

        # worker 的 processed 统计是"本批触碰过的消息数"，成功数看 succeeded
        assert stats.processed == 2
        assert stats.failed == 2          # 两条都无法派生（其中一条是坏 JSON）
        assert stats.succeeded == 0
        assert transport.acked == [
            (STREAM, GROUP, "1700000000000-1"),
            (STREAM, GROUP, "1700000000000-2"),
        ], "每条消息都必须用 poll 注入的 id 被 ACK"


class TestWorkerSourceAdapter:
    """适配层：本类返回 {id, payload:<整份文档>}，worker 需要摊平到顶层。"""

    def test_adapter_flattens_document_to_top_level(self):
        """证据：适配后的消息能直接被 worker.py 的 derive_request_from_event 接受。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [("1700000000000-0", stream_fields(requested_event()))])])

        adapted = WorkerSourceAdapter(source).poll(limit=10)

        assert len(adapted) == 1
        assert adapted[0]["id"] == "1700000000000-0"
        assert adapted[0]["envelope"]["event_type"] == "investigation.requested"
        assert adapted[0]["payload"]["investigation_run_id"] == "run-001"
        # 真跑一遍 worker 的派生器：字段语义对不上就会在这里抛 ValueError
        request = derive_request_from_event(adapted[0])
        assert request.investigation_run_id == "run-001"
        assert request.service_hint == "order-service"

    def test_adapter_keeps_malformed_message_auditable(self):
        """坏消息适配后仍然可审计，且带 id（worker 要靠它 ACK 掉）。"""
        source, transport = make_source()
        transport.queue_read([(STREAM, [("1700000000000-1", stream_fields("{broken"))])])

        adapted = WorkerSourceAdapter(source).poll(limit=10)

        assert adapted[0]["id"] == "1700000000000-1"
        assert adapted[0]["malformed"] is True
        assert adapted[0]["raw"] == "{broken"
        # 解析错误原因一并透传：告警时能直接看到是哪一类坏数据
        assert "error" in adapted[0]

    def test_adapter_streams_id_wins_over_document_id(self):
        """文档里若有顶层 id（非契约字段但可能被误写），不能覆盖 ACK 用的 entry id。"""
        source, transport = make_source()
        event = requested_event()
        event["id"] = "document-supplied-id"
        transport.queue_read([(STREAM, [("1700000000000-0", stream_fields(event))])])

        adapted = WorkerSourceAdapter(source).poll(limit=10)[0]

        assert adapted["id"] == "1700000000000-0"
        assert adapted["payload"]["investigation_run_id"] == "run-001"

    def test_adapter_forwards_ack_to_underlying_source(self):
        source, transport = make_source()

        WorkerSourceAdapter(source).ack("1700000000000-0")

        assert transport.acked == [(STREAM, GROUP, "1700000000000-0")]


class TestProductionStub:
    def test_unimplemented_transport_fails_loudly(self):
        """生产桩必须明确炸，不能假装实现。"""
        stub = UnimplementedRedisTransport()
        with pytest.raises(NotImplementedError, match="redis"):
            stub.xreadgroup(group=GROUP, consumer=CONSUMER, stream=STREAM, count=10, block_ms=1000)
        with pytest.raises(NotImplementedError, match="redis"):
            stub.xack(stream=STREAM, group=GROUP, entry_id="1-0")
        with pytest.raises(NotImplementedError, match="redis"):
            stub.xautoclaim(stream=STREAM, group=GROUP, consumer=CONSUMER,
                            min_idle_ms=1000, start_id=DEFAULT_RECLAIM_START_ID, count=10)


class TestQuotaAccounting:
    """配额必须按"条目数"算，不能按 XAUTOCLAIM 返回值的 len。

    真实 XAUTOCLAIM 返回 [next_cursor, entries, deleted_ids] —— 三元素。
    之前的实现用 len(claimed) 算剩余配额，得到的是 limit-3 这种荒谬值，
    每轮都会超发；而这个 bug 在简化形状（纯列表）的测试桩下永远不会暴露。
    """

    def _source_with_claimed(self, claimed_entries_count: int):
        from traceops.runtime.redis_source import RedisStreamEventSource

        class Transport:
            def __init__(self):
                self.xreadgroup_counts = []
                self.xautoclaim_calls = 0

            def xautoclaim(self, stream, group, consumer, min_idle_ms, start_id, count):
                self.xautoclaim_calls += 1
                # 真实三元素形状：cursor=0-0（表示扫完了）
                entries = [(f"{i}-0", {"payload": "{}"})
                           for i in range(claimed_entries_count)]
                return ["0-0", entries, []]

            def xreadgroup(self, group, consumer, stream, count, block_ms):
                self.xreadgroup_counts.append(count)
                return []

            def xgroup_create(self, stream, group, *, start_id="0", mkstream=True):
                pass

            def xack(self, stream, group, entry_id):
                return 1

        transport = Transport()
        return RedisStreamEventSource(transport, stream="s", group="g", consumer="c"), transport

    def test_remaining_quota_subtracts_entry_count_not_tuple_length(self):
        source, transport = self._source_with_claimed(10)

        source.poll(limit=50)

        # 认领 10 条后，新消息配额应是 40（而不是 47 或 48）
        assert transport.xreadgroup_counts == [40], transport.xreadgroup_counts

    def test_all_quota_consumed_by_claimed_skips_read(self):
        source, transport = self._source_with_claimed(50)

        source.poll(limit=50)

        assert transport.xreadgroup_counts == [], "no quota left -> must not call XREADGROUP"

    def test_zero_claimed_uses_full_quota(self):
        source, transport = self._source_with_claimed(0)

        source.poll(limit=50)

        assert transport.xreadgroup_counts == [50]
