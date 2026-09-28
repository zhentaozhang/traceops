"""Redis Streams 事件源：把 `InvestigationWorker` 接到真实 Redis Streams 上。

## 为什么用"可替换传输层"，而不是在类里 import redis

项目刻意只用标准库 + pydantic + jsonschema + langgraph（见 pyproject.toml），
不引入 redis-py 是**有意为之**：事件源这一层的职责是"把 Streams 的一批原始
entry 变成工作流能吃的消息"，而 RESP 协议、连接池、重连、超时属于传输层。
把两者切开有两个实际收益：

1. 测试不需要跑 Redis 进程：`ScriptedTransport` 按脚本返回批次，
   崩溃恢复（XAUTOCLAIM）、重复投递、坏 JSON 这些**难在真实环境里复现**的
   路径，全部可以确定性地测。
2. 生产接入不需要改这个类：接 `redis-py`、或自己写一个 RESP 客户端、
   或用官方 `redis` CLI 的管道封装，都只要满足 `RedisCommandTransport`。

代价是：仓库里目前**没有**真正能连 Redis 的传输层实现。
`UnimplementedRedisTransport` 只是一个"明确还不支持"的桩，调用即抛
`NotImplementedError` —— 宁可启动时报错，也不能用一个假的实现让上层以为
已经接通（P0 末尾的诚实度要求）。

## 为什么 poll 必须用 XAUTOCLAIM

Redis Streams 的消费组只在"条目已投递但未 ACK"的 pending 状态里保留进度。
进程被杀时，那批已 XREADGROUP 拿到、但没来得及 XACK 的消息会留在 PEL 里。
如果只 XREADGROUP 新消息：
- 崩溃前那批永远没人处理（PEL 越积越多）；
- 顺序也会错乱 —— 新事件先跑，被中断的调查最后才补。

所以 poll 先 XAUTOCLAIM 拿回 pending（`min_idle_ms` 门限避免抢正在跑的消息），
再 XREADGROUP 读新消息。恢复优先是**顺序保证**，不是优化。

## 关于 xdel 的取舍

Transport Protocol 里没有 `xdel`：消费完成就删条目会让 Stream 失去审计和
重放能力，裁剪应该由生产端的 `XADD ... MAXLEN` / 运维的 `XTRIM` 负责，
不该在消费端偷偷做。

## 与 worker.py 的消息形状

本类的 `poll` 返回 `{"id": entry_id, "payload": <解析后的事件文档>}`。
`InvestigationWorker` 的 `derive_request_from_event` 则期望把事件文档摊平到
顶层（`message["payload"]` 是事件 payload、`message["envelope"]` 是信封）。
两者不是同一个形状，因此提供 `WorkerSourceAdapter` 做边界适配，
worker.py 一行都不用改（详见该类 docstring）。
"""
from __future__ import annotations

import json
from typing import Any, Protocol

DEFAULT_RECLAIM_START_ID = "0-0"
PAYLOAD_FIELD = "payload"


class RedisCommandTransport(Protocol):
    """Streams 命令的抽象传输层。

    实现方只需负责"把命令发出去、把 RESP 结果转成 Python 结构"；
    重连/重试/凭证都在这一层内部处理，上层看到的只是这三个返回值。
    返回形状刻意选了 Python 原生结构（而不是 RESP 原始数组），
    这样测试可以用纯 dict/list 构造数据。
    """

    def xreadgroup(self, group: str, consumer: str, stream: str, count: int,
                   block_ms: int) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        """`XREADGROUP GROUP g c COUNT n BLOCK ms STREAMS s >` 的结果。

        返回 `[(stream_key, [(entry_id, fields), ...]), ...]`；
        没有新消息时返回空列表（Redis 的超时表现为空，不是异常）。
        """
        ...

    def xack(self, stream: str, group: str, entry_id: str) -> int:
        """`XACK s g id`，返回被确认的条目数（0 表示 id 不在 PEL 里）。"""
        ...

    def xautoclaim(self, stream: str, group: str, consumer: str, min_idle_ms: int,
                   start_id: str, count: int) -> Any:
        """`XAUTOCLAIM s g c min-idle-time start COUNT n` 能被重试的 pending 条目。

        返回形状：**真实 Redis 的三元素** `[next_cursor, [(entry_id, fields), ...],
        [deleted_id, ...]]`。允许简化形状（纯条目列表），由 `_extract_cursor` /
        `_extract_entries` 统一兼容。

        为什么注解是 `Any` 而不是具体类型：两种形状都合法，写死成 `list[tuple[...]]`
        会让真实实现（返回三元组）在类型上与契约冲突 —— 这正是此前暴露的问题。
        真实的 XAUTOCLAIM 还会返回"下一个游标"和"被删除但仍留 PEL 的 id"，
        本层暂时只需要前一部分；实现方可以把他们丢掉，但**不要**把已删除的
        id 当成正常消息伪装回来（见 `UnimplementedRedisTransport` 的注释）。
        """
        ...


class UnimplementedRedisTransport:
    """生产接入桩：接口已就绪，但仓库里没有 RESP 客户端。

    为什么留一个会抛异常的类而不是删掉：在没有实现之前，如果这里静默返回空列表，
    上层会看到"Redis 一直没消息"，把接入失败伪装成"没有流量"，
    排查成本极高。明确失败、明确报错，才是诚实。
    """

    _MESSAGE = ("未实现：接入真实 Redis 需要满足 RedisCommandTransport 的传输层。"
                "请用 redis-py / 自写 RESP 客户端实现 xreadgroup、xack、xautoclaim 后注入，"
                "本层不负责网络与协议细节。")

    def xreadgroup(self, group: str, consumer: str, stream: str, count: int,
                   block_ms: int) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        raise NotImplementedError(self._MESSAGE)

    def xgroup_create(self, stream: str, group: str, *, start_id: str = "0",
                      mkstream: bool = True) -> None:
        """`XGROUP CREATE <s> <g> <start> MKSTREAM`；已存在时抛 BUSYGROUP。"""
        ...

    def xack(self, stream: str, group: str, entry_id: str) -> int:
        raise NotImplementedError(self._MESSAGE)

    def xautoclaim(self, stream: str, group: str, consumer: str, min_idle_ms: int,
                   start_id: str, count: int) -> list[tuple[str, dict[str, str]]]:
        raise NotImplementedError(self._MESSAGE)


def _require_text(name: str, value: Any) -> str:
    """配置项校验：空 group/consumer 会让命令打到不存在的消费者组，启动即失败更好。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value.strip()


def decode_entry(entry_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """把一条 Streams entry 变成消息字典，**永远带上 id**。

    坏消息为什么保留：`payload` 字段不是合法 JSON 通常意味着上游 producer
    升级/编码失误或写入了非本类型事件。静默丢弃的结果是"线上没消息了"，
    运维无法区分"没流量"和"被判死刑的消息"。保留原文字段 + `malformed=True`
    让 ACK、审计、告警三条路径都还成立（坏消息也要 ACK，否则它会在 PEL 里
    被反复认领，把消费组卡死）。
    """
    raw = fields.get(PAYLOAD_FIELD)
    if raw is None:
        # 字段都不全：把整个 entry 序列化进 raw，至少还能现场取证
        return {
            "id": entry_id,
            "malformed": True,
            "raw": json.dumps(fields, ensure_ascii=False, sort_keys=True),
            "error": f"missing {PAYLOAD_FIELD!r} field",
        }
    try:
        document = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {"id": entry_id, "malformed": True, "raw": raw, "error": str(exc)}
    if not isinstance(document, dict):
        # JSON 合法但不是对象（数组/数字）：同样不是本契约的事件
        return {"id": entry_id, "malformed": True, "raw": raw,
                "error": f"payload must be a JSON object, got {type(document).__name__}"}
    return {"id": entry_id, "payload": document}


class RedisStreamEventSource:
    """满足 worker.py 的 `EventSource` Protocol 的 Redis Streams 实现。

    以下三条是本类对上层的行为承诺（测试逐条钉住）：

    1. 每条消息都带 `"id"`：worker 用它 XACK，没有 id 的消息等于永不确认；
    2. pending（XAUTOCLAIM 认领回来的）排在批次最前面：崩溃恢复优先；
    3. 解析失败的消息**不丢**：带 `malformed=True` 和 `raw` 返回，可审计、可 ACK。
    """

    def __init__(self, transport: RedisCommandTransport, *, stream: str, group: str,
                 consumer: str, block_ms: int = 1000, min_idle_ms: int = 60_000) -> None:
        self.transport = transport
        self.stream = _require_text("stream", stream)
        self.group = _require_text("group", group)
        self.consumer = _require_text("consumer", consumer)
        if block_ms < 0:
            raise ValueError(f"block_ms must be >= 0, got {block_ms!r}")
        if min_idle_ms < 0:
            raise ValueError(f"min_idle_ms must be >= 0, got {min_idle_ms!r}")
        self.block_ms = block_ms
        self.min_idle_ms = min_idle_ms
        # XAUTOCLAIM 游标：跨轮保留，避免每轮重扫 PEL 头部导致尾部饿死
        self._claim_cursor: str = DEFAULT_RECLAIM_START_ID
        #: 消费者组是否已确保存在。只做一次 —— 每轮都建组是白费往返。
        self._group_ready = False

    def lag_report(self) -> dict[str, Any]:
        """消费者积压报告（**只读**，不改变任何消费状态）。

        为什么需要：没有它，一个静默追赶不上的消费者与一个健康的空转消费者
        在外部**完全无法区分** —— 两者都表现为"Redis 内存慢慢涨"。

        三个字段的含义：

        | 字段 | 含义 | 异常时的含义 |
        |---|---|---|
        | `lag` | 尚未投递的消息数 | 持续增长 = 消费跟不上生产 |
        | `pending` | 已投递但未 ACK | 持续增长 = worker 在处理中崩溃残留 |
        | `consumers` | 在线消费者数 | 为 0 而 lag 在涨 = worker 全挂了 |

        `lag` 为 `None` 而不是 `0` 表示**组不存在** —— 报 0 会伪装成
        "完全跟上了"，那是比没有指标更糟的误导。
        """
        groups = []
        try:
            groups = self.transport.xinfo_groups(self.stream)
        except Exception:
            groups = []

        for group in groups:
            if str(group.get("name", "")) == self.group:
                lag = group.get("lag")
                if lag is None and group.get("pending") is None:
                    lag = None
                return {
                    "stream": self.stream,
                    "group": self.group,
                    "lag": int(lag) if isinstance(lag, (int, float)) else None,
                    "pending": int(group.get("pending", 0) or 0),
                    "consumers": int(group.get("consumers", 0) or 0),
                }
        # 组不存在：明确表达"没有数据"，而不是 0
        return {
            "stream": self.stream,
            "group": self.group,
            "lag": None,
            "pending": None,
            "consumers": 0,
        }

    def _ensure_group(self) -> None:
        """确保消费者组存在（幂等）。

        为什么必须有这一步：Java 侧只做 `XADD`（生产者不建组，这是 Redis
        Streams 的正常分工），而本类直接 `XREADGROUP`。于是**全新部署**里
        读取必然报 `NOGROUP No such key ... or consumer group ...`，
        看起来像"Redis 有问题"，实际是少了一个初始化步骤。

        三个细节决定正确性：

        1. `MKSTREAM`：流还不存在时也要能建组（否则首个消息到来前永远建不了）
        2. 起始位置用 **`0` 而不是 `$`**：`$` 从"现在"开始，会静默跳过
           **已积压**的事件 —— 而首次部署最需要处理的正是那些。
           运维看到的症状是"事故建了但永远没结论"，日志里却一切正常。
        3. `BUSYGROUP` 必须吞掉：组已存在是正常情况（重启、多 worker 并发）。
        """
        if self._group_ready:
            return
        try:
            # 走**公开**接口而不是 `_command`：私有命令让测试替身必须模仿
            # 内部细节，既脆弱又逼人抄实现。传输层只需暴露 xgroup_create。
            self.transport.xgroup_create(
                self.stream, self.group, start_id="0", mkstream=True)
        except Exception as exc:
            text = str(exc).upper()
            if "BUSYGROUP" not in text:
                raise
            # 已存在 → 正常，继续
        self._group_ready = True

    def poll(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """先认领 pending，再读新消息；合计不超过 limit。"""
        if limit <= 0:
            return []

        self._ensure_group()
        messages: list[dict[str, Any]] = []
        # 1) 崩溃恢复：把本 consumer 名下"超时未 ACK"的 pending 拿回来。
        #    游标必须跨轮保留：真实 Redis 的 XAUTOCLAIM 返回 next_cursor，
        #    每轮都从 0-0 重新扫会让 PEL 很大时的尾部永远轮不到（饿死）。
        claimed = self.transport.xautoclaim(
            self.stream, self.group, self.consumer,
            self.min_idle_ms, self._claim_cursor, limit,
        )
        self._claim_cursor = self._extract_cursor(claimed)
        claimed_entries = self._extract_entries(claimed)
        for entry_id, fields in claimed_entries:
            messages.append(decode_entry(entry_id, fields))

        # 2) 剩下的配额读新消息。count 取剩余额度，避免一次 poll 超过 limit。
        #
        # 注意：**必须用条目数而不是 len(claimed)**。真实 XAUTOCLAIM 返回的是
        # [next_cursor, entries, deleted_ids] 三元组，len() 恒为 3 ——
        # 之前这里算成 limit-1，每轮都超发配额（还会随 cursor/deleted 长度漂移）。
        remaining = limit - len(claimed_entries)
        if remaining > 0:
            streams = self.transport.xreadgroup(
                self.group, self.consumer, self.stream, remaining, self.block_ms,
            )
            for _stream_key, entries in streams:
                for entry_id, fields in entries:
                    messages.append(decode_entry(entry_id, fields))
        return messages

    # ---- XAUTOCLAIM 返回形状兼容 ----
    # 真实 Redis 返回 [next_cursor, [entry...], [deleted_id...]]；
    # 轻量桩可能只返回 [entry...]。两种都要能处理，且游标必须被保留。
    def _extract_cursor(self, claimed: Any) -> str:
        if isinstance(claimed, (list, tuple)) and len(claimed) == 3 and isinstance(claimed[0], str):
            return claimed[0] or DEFAULT_RECLAIM_START_ID
        return DEFAULT_RECLAIM_START_ID

    def _extract_entries(self, claimed: Any) -> list[tuple[str, dict[str, str]]]:
        if isinstance(claimed, (list, tuple)) and len(claimed) == 3 and isinstance(claimed[0], str):
            return list(claimed[1] or [])  # 真实形状
        return list(claimed or [])          # 简化形状

    def ack(self, message_id: str) -> None:
        """XACK 单条消息。stream/group 由构造时配置决定，所以调用方只需要 id。"""
        self.transport.xack(self.stream, self.group, message_id)


class WorkerSourceAdapter:
    """把本类的消息形状适配成 `worker.py` 期望的顶层形状。

    为什么要适配而不是改 worker：`derive_request_from_event` 读
    `message["payload"]` 作为事件 payload、`message["envelope"]` 作为信封，
    这是 contracts/ 定义的字段语义；Redis entry 里却只有**一个** `payload`
    field，里面装着整份文档。把"Streams entry 的形状"和"事件消息的形状"
    混在一起，会让 worker 误以为事件 payload 里还有 envelope，宁可多一层
    显式适配，也不去动已经测过的 worker。

    用法：

        source = RedisStreamEventSource(transport, stream=..., group=..., consumer=...)
        worker = InvestigationWorker(source=WorkerSourceAdapter(source), sink=...)

    malformed 消息保持原样透传：worker 派生失败后会产出
    `INVALID_REQUEST` 的 failed 事件并 ACK，这正是审计需要的行为。
    """

    def __init__(self, source: RedisStreamEventSource) -> None:
        self._source = source

    def poll(self, *, limit: int = 50) -> list[dict[str, Any]]:
        adapted: list[dict[str, Any]] = []
        for message in self._source.poll(limit=limit):
            if message.get("malformed"):
                adapted.append({key: value for key, value in message.items()
                                if key in ("id", "malformed", "raw", "error")})
                continue
            # 摊平：event_id 之外不保留任何顶层冲突字段。id 放最后注入，
            # 保证"Streams entry id"永远压过文档里可能存在的同名字段 ——
            # worker 的 ACK 依赖这个 id，被文档覆盖就等于 ACK 打错条目标。
            adapted.append({**message["payload"], "id": message["id"]})
        return adapted

    def ack(self, message_id: str) -> None:
        self._source.ack(message_id)
