"""手写 RESP2 客户端：`RedisCommandTransport` 的真实实现。

## 为什么自己写，而不是引入 redis-py

`pyproject.toml` 只允许标准库 + pydantic + jsonschema + langgraph，引入 redis-py
会同时引入 hiredis/连接池/重试策略这一整套东西，而 `RedisStreamEventSource`
真正需要的只有三条命令（XREADGROUP / XACK / XAUTOCLAIM）。RESP2 的报文格式
小到可以在一百行内正确实现，把「协议」留在仓库里，反而比多一个依赖更好审计：

- 每条命令发出去的是什么字节、回包怎么被切成值，都能在读代码时确认；
- 不需要为「redis-py 的某个版本把 XAUTOCLAIM 的 third element 怎么处理」翻文档；
- 测试不依赖 Redis 进程：`RespParser` 可以逐块喂字节，`socketpair()` 可以
  扮演服务端（见 `tests/runtime/test_redis_transport.py`）。

## 流式解析是本文件的核心

socket 是字节流，**一次 recv 可能只拿到半行、半条 bulk string、半个数组**。
所以这里不做「先读够一整包再解析」，而是维护一个 `bytearray` 缓冲：
需要什么就先确保缓冲里有（`_ensure_bytes`），不够就向 `fill` 回调再要一块。
几个具体后果，也是最容易写错的地方：

1. 行（`+`/`-`/`:`/`$`/`*` 的头部）以 CRLF 结束，但 CRLF 可能被拆在两个
   chunk 里（一个 chunk 停在 `\\r`）。`_read_line` 用 `find(b"\\r\\n")`
   **循环等待**，而不是「读到 CRLF 就假设整行到了」。
2. bulk string 的正文长度由头部声明，正文之后还有 2 字节 CRLF，也必须
   等齐（`$5\\r\\nhel` + 后续 `lo\\r\\n` 才算完整）。
3. 嵌套数组用**递归**解析，子元素共享同一个缓冲区；如果改成「先把数组头部
   读出来，后面单独 parse」，头部与正文就会错位。
4. 解析过的字节立刻从缓冲区移除，剩下的字节**留给下一条命令**：
   一次 recv 拿到两条回包（管道/预写测试）时不会多读也不会丢失。
5. 对端关闭（recv 返回 `b""`）时抛 `RedisConnectionError`，绝不返回
   「半个值」——半个值会让上层把协议错位当成业务数据。

## 与 `redis_source.py` 的契约

返回形状严格按 `RedisCommandTransport`：

- `xreadgroup` → `[(stream_key, [(entry_id, fields_dict), ...])]`（超时回空列表）
- `xack` → `int`
- `xautoclaim` → **三元素** `[next_cursor, [(entry_id, fields_dict), ...], [deleted_id, ...]]`

Redis 的 XREADGROUP / XAUTOCLAIM 把 entry 的字段给成 `[field, value, field,
value, ...]` 的扁平静态数组，这里必须转成 dict，否则上层的
`fields.get("payload")` 只能靠下标猜，多一个字段就全错。
"""
from __future__ import annotations

import socket
from typing import Any, Callable

CRLF = b"\r\n"
_RECV_SIZE = 65536
# 严格模式：Redis 只会返回协议定义的前缀，多一个前缀说明连接已经错位，
# 与其猜着继续读，不如立刻让连接失效（见 `_read_value`）。
_SIMPLE_STRING = b"+"
_ERROR = b"-"
_INTEGER = b":"
_BULK_STRING = b"$"
_ARRAY = b"*"


# --------------------------------------------------------------------------- #
# 异常：区分「命令被拒」和「连接坏了」是错误处理的第一步
# --------------------------------------------------------------------------- #
class RedisTransportError(Exception):
    """传输层异常基类。上层通常只需要区分下面两个具体类型。"""


class RedisConnectionError(RedisTransportError):
    """连接不可用：建立失败、被对端关闭、socket 错误、协议错位。

    为什么协议错位也算连接错误：RESP 没有帧同步机制，一旦读错一个字节，
    后续所有回包都会错位。这种状态下的连接**不能复用**，必须丢弃重建。
    """


class RedisCommandError(RedisTransportError):
    """Redis 用 `-ERR ...` 明确拒绝了一条命令，但**连接仍然可用**。

    为什么单列一个类型：`NOPERM`、`NOGROUP`（消费者组不存在）、
    `ERR unknown command` 都是「命令层面的失败」，把它们当成连接错误去重连，
    只会把真正的配置问题（组名拼错）伪装成网络抖动。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RedisProtocolError(RedisConnectionError):
    """回包形状不符合协议（比如 entry 的字段列表长度是奇数）。

    继承 `RedisConnectionError` 而不是独立类型：形状不符合意味着读指针
    已经不可信，连接必须重建；调用方也只需要按「连接坏了」处理。
    """


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
def _xinfo_groups_to_dicts(raw: Any) -> list[dict[str, Any]]:
    """`XINFO GROUPS` 的 RESP 数组 → 平铺 dict 列表。

    Redis 返回的每条组是**交替键值对的数组**：

        ["name", "g", "consumers", 1, "pending", 0, "lag", 7, ...]

    不是一个 dict。直接按 dict 处理会静默得到空结果 ——
    那会让监控显示"一切正常"，而实际积压可能很大。
    这里显式做键值对拆分，缺 `lag` 字段时给 None（而不是 0）。
    """
    if not isinstance(raw, list):
        return []
    groups: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, (list, tuple)):
            continue
        blob = {}
        # 交替键值：步长 2
        for i in range(0, len(entry) - 1, 2):
            key = entry[i]
            value = entry[i + 1]
            if isinstance(key, bytes):
                key = key.decode("utf-8", errors="replace")
            blob[str(key)] = value
        groups.append(blob)
    return groups


def encode_command(args: "list[Any] | tuple[Any, ...]") -> bytes:
    """把一条命令编码成 RESP2 数组：`*<n>\\r\\n$<len>\\r\\n<arg>\\r\\n...`。

    为什么长度用 `len(bytes)` 而不是 `len(str)`：RESP 的 `$<len>` 是**字节数**，
    非 ASCII 参数（中文 payload、emoji）按字符数算会直接让服务端读错位。
    统一先 `encode("utf-8")` 再取长度，是唯一不会错的做法。
    """
    if not args:
        raise ValueError("RESP 命令至少需要一个参数（命令名本身）")

    parts: list[bytes] = [b"*%d\r\n" % len(args)]
    for arg in args:
        if isinstance(arg, bytes):
            chunk = arg
        elif isinstance(arg, str):
            chunk = arg.encode("utf-8")
        else:
            # int 等标量：RESP 没有独立的数字参数类型，统一按字符串发
            chunk = str(arg).encode("utf-8")
        parts.append(b"$%d\r\n" % len(chunk))
        parts.append(chunk)
        parts.append(b"\r\n")
    return b"".join(parts)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _decode_text(data: bytes) -> str:
    """bytes → str。为什么用 errors="replace"：上层契约是 str，坏字节不能
    让整批消息原地爆炸；替换字符会出现在 `raw` 里，仍然可审计。"""
    return data.decode("utf-8", errors="replace")


def _parse_integer(line: bytes) -> int:
    try:
        return int(line)
    except ValueError as exc:
        raise RedisProtocolError(f"RESP 整数字段无法解析: {line!r}") from exc


class RespParser:
    """RESP2 值的增量解析器。

    构造时传入 `fill`：缓冲区不足时调用它取更多字节（返回 `b""` = 对端关闭）。
    真实场景 `fill` 就是 `socket.recv`；测试里可以是「按列表弹出下一块」，
    所以「分片到达」这件事能被确定性验证，而不是靠网络运气。

    也可以不传 `fill`，改用 `feed()` 手动塞字节；此时取不够就抛
    `RedisConnectionError`（表示数据没喂完），适合纯解析单测。
    """

    def __init__(self, fill: Callable[[], bytes] | None = None) -> None:
        self._fill = fill
        self._buffer = bytearray()

    # ---- 供测试 / 外部预置数据 ----
    def feed(self, data: bytes) -> None:
        """把已经拿到的字节放进缓冲区（可与 `read_reply` 交替调用）。"""
        if data:
            self._buffer.extend(data)

    # ---- 内部：确保缓冲区有足够字节 ----
    def _pull_more(self) -> None:
        data = self._fill() if self._fill is not None else b""
        if not data:
            # EOF：宁可抛错也不能返回半个值（半个值等于把协议错位当数据）
            raise RedisConnectionError("读取 Redis 响应时连接被对端关闭（EOF）")
        self._buffer.extend(data)

    def _ensure_bytes(self, size: int) -> None:
        while len(self._buffer) < size:
            self._pull_more()

    def _read_line(self) -> bytes:
        """读一行（不含尾部 CRLF）。

        关键点：CRLF 可能被切在两个 recv 之间，所以这里**循环**到 find 成功为止，
        而不是「缓冲区里有字节就当作一行」。
        """
        while True:
            index = self._buffer.find(CRLF)
            if index != -1:
                line = bytes(self._buffer[:index])
                del self._buffer[:index + 2]
                return line
            self._pull_more()

    # ---- 单个值 ----
    def read_reply(self) -> Any:
        """解析一个完整的回包值（`read_reply` 之间共享缓冲区）。"""
        return self._read_value()

    def _read_value(self) -> Any:
        self._ensure_bytes(1)
        prefix = bytes(self._buffer[:1])
        del self._buffer[:1]

        if prefix == _SIMPLE_STRING:
            return _decode_text(self._read_line())
        if prefix == _ERROR:
            # 错误也是「一个值」，但协议语义是命令失败：抛出去让调用方处理
            raise RedisCommandError(_decode_text(self._read_line()))
        if prefix == _INTEGER:
            return _parse_integer(self._read_line())
        if prefix == _BULK_STRING:
            return self._read_bulk_string()
        if prefix == _ARRAY:
            return self._read_array()
        raise RedisProtocolError(f"无法识别的 RESP 前缀: {prefix!r}")

    def _read_bulk_string(self) -> str | None:
        length = _parse_integer(self._read_line())
        if length < 0:
            # `$-1` 是 RESP2 的 null bulk string（值不存在），不是空字符串
            return None
        # +2：正文后面固定跟一个 CRLF，它可能还没到
        self._ensure_bytes(length + 2)
        payload = bytes(self._buffer[:length])
        del self._buffer[:length + 2]
        return _decode_text(payload)

    def _read_array(self) -> list[Any] | None:
        count = _parse_integer(self._read_line())
        if count < 0:
            # `*-1` 是 null array（BLOCK 超时时 XREADGROUP 就回这个）
            return None
        # 递归解析：子元素继续从**同一个**缓冲区取，天然支持嵌套数组
        return [self._read_value() for _ in range(count)]


# --------------------------------------------------------------------------- #
# Python 结构转换：RESP 原始形状 → Protocol 要求的形状
# --------------------------------------------------------------------------- #
def _to_text(value: Any) -> str:
    """把 RESP 值统一成字符串。

    为什么 None 变成空串：entry 的字段在 Redis 里永远是 bulk string，真出现
    null 只可能是数据被写坏；上层 `decode_entry` 对缺 `payload` 的 entry 有
    专门的 malformed 路径（保留原文 + 可 ACK），给空串比抛异常更容易审计。
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return _decode_text(value)
    return value if isinstance(value, str) else str(value)


def _fields_to_dict(raw: Any) -> dict[str, str]:
    """`[field, value, field, value, ...]` 扁平数组 → dict。

    为什么必须校验偶数长度：奇数长度意味着这次读取已经错位（或者上游写了
    非法 entry），硬凑成 dict 会把错位数据当成合法 payload 交给工作流，
    比直接断连危险得多。
    """
    if not isinstance(raw, (list, tuple)):
        raise RedisProtocolError(f"entry 的字段应为数组，实际 {type(raw).__name__}")
    if len(raw) % 2 != 0:
        raise RedisProtocolError(f"entry 的字段列表长度为奇数({len(raw)})，协议已错位")
    fields: dict[str, str] = {}
    for index in range(0, len(raw), 2):
        fields[_to_text(raw[index])] = _to_text(raw[index + 1])
    return fields


def _entries_from_raw(raw_entries: Any) -> list[tuple[str, dict[str, str]]]:
    """`[[id, [field, value, ...]], ...]` → `[(id, {field: value}), ...]`。"""
    if not isinstance(raw_entries, (list, tuple)):
        raise RedisProtocolError(
            f"entry 列表应为数组，实际 {type(raw_entries).__name__}")
    entries: list[tuple[str, dict[str, str]]] = []
    for item in raw_entries:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise RedisProtocolError(f"entry 应为 [id, fields] 二元组，实际 {item!r}")
        entry_id, raw_fields = item
        entries.append((_to_text(entry_id), _fields_to_dict(raw_fields)))
    return entries


def _streams_from_raw(reply: Any) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
    """XREADGROUP 回包 → `[(stream_key, [(entry_id, fields), ...])]`。"""
    if not isinstance(reply, (list, tuple)):
        raise RedisProtocolError(f"XREADGROUP 回包应为数组，实际 {type(reply).__name__}")
    streams: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
    for item in reply:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise RedisProtocolError(
                f"XREADGROUP 的 stream 项应为 [key, entries]，实际 {item!r}")
        stream_key, raw_entries = item
        streams.append((_to_text(stream_key), _entries_from_raw(raw_entries)))
    return streams


# --------------------------------------------------------------------------- #
# 参数校验：把 Redis 会报 `-ERR` 的情况提前成 ValueError，错误信息更准确
# --------------------------------------------------------------------------- #
def _require_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value.strip()


def _require_int(name: str, value: Any, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    return value


# --------------------------------------------------------------------------- #
# 传输层
# --------------------------------------------------------------------------- #
class RespRedisTransport:
    """通过一条 TCP 连接实现 `RedisCommandTransport` 的 RESP2 客户端。

    连接是**显式**的：`connect()` 或用 `with`。为什么不做懒加载（第一次发命令
    才发现连不上）：启动时失败能立刻暴露配置问题；而且懒加载会让
    「未连接就调用」变成「悄悄连 localhost:6379」，这是把部署错误伪装成
    「没消息」的经典做法。

    一个 transport 一条连接：不做连接池。`RedisStreamEventSource.poll` 是串行
    调用，池化只增加状态；需要并发时给每个 worker 一个 transport 更简单。
    """

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0,
                 password: str | None = None, timeout: float = 5.0) -> None:
        self.host = _require_text("host", host)
        self.port = _require_int("port", port, minimum=1)
        if self.port > 65535:
            raise ValueError(f"port must be <= 65535, got {self.port!r}")
        self.db = _require_int("db", db, minimum=0)
        if password is not None and not isinstance(password, str):
            raise ValueError(f"password must be a str or None, got {type(password).__name__}")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError(f"timeout must be a number, got {timeout!r}")
        if timeout <= 0:
            raise ValueError(f"timeout must be > 0, got {timeout!r}")
        self.password = password
        self.timeout = float(timeout)

        self._sock: socket.socket | None = None
        self._parser: RespParser | None = None

    # ---- 连接生命周期 ----

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> "RespRedisTransport":
        """建立连接，并在返回前完成 AUTH / SELECT。

        为什么 AUTH/SELECT 失败要关掉整个连接：半配置成功的连接会让后续命令
        打到一个没选库、或没权限的实例上，错误会晚很多才暴露；
        `connect()` 要么完整成功，要么不留痕迹。
        """
        if self._sock is not None:
            return self
        sock = self._open_socket()
        self._adopt_socket(sock)
        try:
            if self.password is not None:
                self._command("AUTH", self.password)
            if self.db != 0:
                self._command("SELECT", self.db)
        except BaseException:
            self.close()
            raise
        return self

    def _open_socket(self) -> socket.socket:
        """单独一个方法只为可测试：单测可替换成 `socketpair()` 的一端。"""
        try:
            return socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise RedisConnectionError(
                f"无法连接 Redis {self.host}:{self.port}: {exc}") from exc

    def _adopt_socket(self, sock: socket.socket) -> None:
        sock.settimeout(self.timeout)
        self._sock = sock
        self._parser = RespParser(fill=self._recv)

    def _recv(self) -> bytes:
        """解析器要字节时调这里。EOF 交给解析器判断（返回 b""）。"""
        if self._sock is None:
            raise RedisConnectionError(self._not_connected_message())
        try:
            return self._sock.recv(_RECV_SIZE)
        except socket.timeout as exc:
            raise RedisConnectionError(
                f"读取 Redis {self.host}:{self.port} 响应超时"
                f"（socket timeout={self.timeout}s）") from exc
        except OSError as exc:
            raise RedisConnectionError(
                f"读取 Redis {self.host}:{self.port} 响应失败: {exc}") from exc

    def close(self) -> None:
        """关连接。幂等：`__exit__` 和 finally 同时调用也不会炸。"""
        sock, self._sock, self._parser = self._sock, None, None
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            # 关闭阶段的错误没有处理价值：连接已经不可用了
            pass

    def __enter__(self) -> "RespRedisTransport":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False  # 不吞异常

    # ---- 命令通路 ----

    def _not_connected_message(self) -> str:
        return (f"尚未连接 Redis {self.host}:{self.port}："
                f"请先调用 connect()（或用 with 语句）再发命令")

    def _require_socket(self) -> tuple[socket.socket, RespParser]:
        if self._sock is None or self._parser is None:
            # 明确的连接错误，而不是 AttributeError/None.recv
            raise RedisConnectionError(self._not_connected_message())
        return self._sock, self._parser

    def _command(self, *args: Any) -> Any:
        sock, parser = self._require_socket()
        payload = encode_command(args)
        try:
            sock.sendall(payload)
            return parser.read_reply()
        except RedisCommandError:
            # `-ERR` 是业务失败：连接状态仍然干净，继续用
            raise
        except RedisConnectionError:
            self.close()  # 读错位/EOF/超时后连接不可信，丢弃等重建
            raise
        except OSError as exc:
            self.close()
            raise RedisConnectionError(
                f"与 Redis {self.host}:{self.port} 通信失败: {exc}") from exc

    def _blocking_command(self, block_ms: int, *args: Any) -> Any:
        """执行带 `BLOCK` 的命令，期间放宽 socket 超时。

        为什么必须放宽：`BLOCK 1000` 时服务端会故意 1 秒不回应，
        socket 的固定 5s 超时本身没问题，但把超时设成「阻塞时长 + 常规超时」
        才是对的两个量；`block_ms == 0` 表示无限阻塞，此时只能把超时设为
        `None`。若在阻塞中被 socket 超时打断，我们可能已经发出命令、
        又读到半个回包，连接必须丢弃——所以这里宁可把窗口留足。
        """
        sock, _parser = self._require_socket()
        previous = sock.gettimeout()
        sock.settimeout(None if block_ms == 0 else self.timeout + block_ms / 1000.0)
        try:
            return self._command(*args)
        finally:
            # 连接可能已被 `_command` 关闭；只在还活着时恢复
            current = self._sock
            if current is not None:
                try:
                    current.settimeout(previous)
                except OSError:
                    pass

    # ---- RedisCommandTransport ----

    def xreadgroup(self, group: str, consumer: str, stream: str, count: int,
                   block_ms: int) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        """`XREADGROUP GROUP <g> <c> COUNT <n> BLOCK <ms> STREAMS <s> >`。

        末尾的 `>` 表示「只要从未投递过的新消息」；pending 由 XAUTOCLAIM 负责，
        两者混在一起会把崩溃恢复的顺序打乱（见 redis_source.py 的说明）。
        """
        group = _require_text("group", group)
        consumer = _require_text("consumer", consumer)
        stream = _require_text("stream", stream)
        count = _require_int("count", count, minimum=1)
        block_ms = _require_int("block_ms", block_ms, minimum=0)

        reply = self._blocking_command(
            block_ms,
            "XREADGROUP", "GROUP", group, consumer, "COUNT", count,
            "BLOCK", block_ms, "STREAMS", stream, ">",
        )
        if reply is None:
            # BLOCK 到超时：Redis 回 `*-1`（null array），这是「没消息」而不是错误
            return []
        return _streams_from_raw(reply)

    def xinfo_groups(self, stream: str) -> list[dict[str, Any]]:
        """`XINFO GROUPS <stream>` → 每个组一条 dict（name/lag/pending/consumers）。

        用于**积压监控**：没有它无法区分"消费跟得上"与"消费静默落后" ——
        两者在外部看都只是"Redis 内存慢慢涨"。
        """
        raw = self._command("XINFO", "GROUPS", stream)
        return _xinfo_groups_to_dicts(raw)

    def xpending_count(self, stream: str, group: str) -> int:
        """`XPENDING <stream> <group>` 的 summary 形式 → 未 ACK 条数。"""
        raw = self._command("XPENDING", stream, group)
        if isinstance(raw, list) and raw:
            try:
                return int(raw[0])
            except (TypeError, ValueError):
                return 0
        return 0

    def xadd(self, stream: str, payload: str, *, max_len: int | None = None) -> str | None:
        """`XADD <stream> [MAXLEN ~ <n>] * payload <json>`，返回条目 id。

        返回 ``None`` 表示**没有拿到回执** —— 事件没有发出去。
        为什么不在这里抛异常：调用方（`RedisStreamEventSink`）要区分
        "没回执"和"命令被 Redis 拒绝"，两种都是失败，但错误信息不同。

        `MAXLEN ~` 是近似裁剪：Redis 只需要在某个节点上裁一次，
        比精确裁剪便宜得多；代价是流的长度可能略微超过 max_len。
        这正是我们要的 —— 宁可多留几十条，也不能为了精确长度付出 O(n) 代价。
        """
        stream = _require_text("stream", stream)
        if not isinstance(payload, str):
            raise ValueError(f"payload must be a str, got {type(payload).__name__}")
        if max_len is not None:
            max_len = _require_int("max_len", max_len, minimum=1)

        args: list[Any] = ["XADD", stream]
        if max_len is not None:
            args += ["MAXLEN", "~", max_len]
        args += ["*", "payload", payload]

        reply = self._command(*args)
        if reply is None:
            return None
        if isinstance(reply, bytes):
            return reply.decode("utf-8", errors="replace")
        if isinstance(reply, str):
            return reply
        raise RedisProtocolError(f"XADD 应返回条目 id 字符串，实际 {reply!r}")

    def xgroup_create(self, stream: str, group: str, *, start_id: str = "0",
                      mkstream: bool = True) -> None:
        """`XGROUP CREATE <s> <g> <start> MKSTREAM`。

        已存在时抛 `RedisCommandError("BUSYGROUP ...")` —— 调用方应视为正常。
        """
        args: list[Any] = ["XGROUP", "CREATE", stream, group, start_id]
        if mkstream:
            args.append("MKSTREAM")
        self._command(*args)

    def xack(self, stream: str, group: str, entry_id: str) -> int:
        """`XACK <stream> <group> <id>`，返回被移出 PEL 的条数。"""
        stream = _require_text("stream", stream)
        group = _require_text("group", group)
        entry_id = _require_text("entry_id", entry_id)

        reply = self._command("XACK", stream, group, entry_id)
        if isinstance(reply, bool) or not isinstance(reply, int):
            raise RedisProtocolError(f"XACK 应返回整数，实际 {reply!r}")
        return reply

    def xautoclaim(self, stream: str, group: str, consumer: str, min_idle_ms: int,
                   start_id: str, count: int) -> list[Any]:
        """`XAUTOCLAIM <s> <g> <c> <min-idle-time> <start> COUNT <n>`。

        回包是**三元组** `[next_cursor, entries, deleted_ids]`。
        为什么三样都返回：`redis_source.py` 要把游标存到下一轮（否则 PEL 尾部
        永远扫不到），并且明确要求「不要把已删除的 id 当成正常消息」。

        注意：Redis 会返回比 `COUNT` 更多的 entry（它在同一次扫描里把
        `start` 之后能认领的都认了），这是协议行为，调用方不该假设恰好 count 条。
        """
        stream = _require_text("stream", stream)
        group = _require_text("group", group)
        consumer = _require_text("consumer", consumer)
        start_id = _require_text("start_id", start_id)
        min_idle_ms = _require_int("min_idle_ms", min_idle_ms, minimum=0)
        count = _require_int("count", count, minimum=1)

        reply = self._command(
            "XAUTOCLAIM", stream, group, consumer, min_idle_ms, start_id,
            "COUNT", count,
        )
        if not isinstance(reply, (list, tuple)) or len(reply) < 2:
            raise RedisProtocolError(
                f"XAUTOCLAIM 应返回 [cursor, entries, deleted]，实际 {reply!r}")

        cursor = _to_text(reply[0])
        entries = _entries_from_raw(reply[1])
        # 第三个元素（被删除但仍在 PEL 的 id）在部分版本/精简响应里可能缺失，
        # 缺就按空列表返回，保证 Protocol 的形状恒定是三元组。
        raw_deleted = reply[2] if len(reply) >= 3 else []
        # 跳过 None：不把「缺失的 id」伪造成空字符串塞进删除列表，
        # 删除列表的语义是「这些 id 已被 XDEL 但仍留在 PEL」，编造内容会误导审计。
        deleted = [_to_text(item) for item in (raw_deleted or []) if item is not None]
        return [cursor, entries, deleted]
