"""RESP2 客户端测试：协议正确性 + 流式解析 + 与 `RedisCommandTransport` 的形状契约。

## 为什么这些用例必须存在

这个文件替代的是「跑一个真实 Redis 再测」——那会带来三个具体问题：
推理慢、需要运维环境、以及**分片**这种真实故障几乎无法确定性复现。
而分片恰恰是手写 RESP 客户端最容易错的地方：socket 不保证一次 recv
拿到一整条回包。所以这里的策略是：

1. 纯解析：`RespParser` 直接喂字节（含「一段响应拆成多块」）；
2. 命令通路：用 `socket.socketpair()` 让一个线程扮演 Redis 服务端，
   客户端走真实 `connect()` → `sendall` → `recv` 的路径，
   这样编码、读取、Python 结构转换都被真实验证，且不依赖外部服务。

**没有**被验证的部分（真实 Redis 才有的行为）在文件末尾的注释里列明，
不假装测过。
"""
from __future__ import annotations

import socket
import threading

import pytest

from traceops.runtime.redis_source import RedisStreamEventSource
from traceops.runtime.redis_transport import (
    RedisCommandError,
    RedisConnectionError,
    RedisProtocolError,
    RespParser,
    RespRedisTransport,
    encode_command,
)

STREAM = "traceops.events"
GROUP = "traceops-agent-runtime"
CONSUMER = "worker-1"


# --------------------------------------------------------------------------- #
# 测试替身：RESP 服务端
# --------------------------------------------------------------------------- #
def split_one_command(buffer: bytes) -> tuple[list[bytes], bytes] | None:
    """从缓冲区里切出一条完整的 RESP 命令；不完整返回 None。

    为什么自己实现而不复用 `RespParser`：`RespParser` 产出的是「值」，
    这里需要的是「命令参数列表」，而且必须能回答「现在还不足一条」，
    不能用 EOF 语义（服务端连接不会关）。
    """
    if not buffer.startswith(b"*"):
        return None
    header_end = buffer.find(b"\r\n")
    if header_end == -1:
        return None
    try:
        arg_count = int(buffer[1:header_end])
    except ValueError:
        raise AssertionError(f"非法 RESP 命令头: {buffer[:header_end + 2]!r}")
    position = header_end + 2
    args: list[bytes] = []
    for _ in range(arg_count):
        if buffer[position:position + 1] != b"$":
            return None
        bulk_end = buffer.find(b"\r\n", position)
        if bulk_end == -1:
            return None
        length = int(buffer[position + 1:bulk_end])
        start = bulk_end + 2
        end = start + length
        # 正文 + 尾部 CRLF 都要到齐，否则这条命令还没收完
        if len(buffer) < end + 2:
            return None
        args.append(buffer[start:end])
        position = end + 2
    return args, buffer[position:]


class ScriptedRedisServer:
    """扮演 Redis：按顺序回放预置的 RESP 响应，并记录收到的命令。

    为什么用一个线程而不是「先写后读」的单线程流程：客户端的
    `sendall` + `read_reply` 是**阻塞**的，服务端不在另一个线程里回话就会死锁。
    线程里所有 socket 都设了超时，测试失败时是断言失败而不是永久挂住。
    """

    def __init__(self, responses: list[bytes], *, timeout: float = 3.0,
                 hangup_after_script: bool = False) -> None:
        client, peer = socket.socketpair()
        self.client_socket = client
        self._peer = peer
        self._responses = list(responses)
        self._timeout = timeout
        # 回放完预置响应后主动断开（用于「收到半个回包后对端关闭」这类场景）。
        # 必须由**服务端**断开而不是测试线程去 close：后者会把客户端 socket 一起关掉，
        # 客户端看到的是 "Bad file descriptor"，而不是我们要测的 EOF 语义。
        self._hangup_after_script = hangup_after_script
        self.commands: list[list[str]] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._peer.settimeout(self._timeout)
        buffer = b""
        try:
            while True:
                chunk = self._peer.recv(65536)
                if not chunk:
                    return  # 客户端关了连接
                buffer += chunk
                while True:
                    parsed = split_one_command(buffer)
                    if parsed is None:
                        break
                    args, buffer = parsed
                    self.commands.append([arg.decode("utf-8") for arg in args])
                    if self._responses:
                        self._peer.sendall(self._responses.pop(0))
                    elif self._hangup_after_script:
                        pass  # 静默：不回复，直接走下面的断开逻辑
                    else:
                        # 没有脚本时回错误而不是沉默：沉默会让客户端阻塞到超时，
                        # 测试失败原因就变成「超时」而不是「少了一条响应」
                        self._peer.sendall(b"-ERR no scripted response\r\n")
                    if self._hangup_after_script and not self._responses:
                        # 预置响应放完就立刻断开。**必须在这里断开**，不能等下一轮
                        # recv（服务端会一直阻塞在 recv 上，客户端则在同一时刻等
                        # 剩下的字节，两边互等，最后只能靠客户端超时才返回）。
                        self._peer.close()
                        return
        except OSError:
            # 线程退出时连接被关掉是正常收尾，不该让 daemon 线程抛异常噪音
            return

    def wait_until_idle(self) -> None:
        """等命令被处理完（最多等一会儿），用于断言服务端收到了哪些命令。"""
        if self._thread.is_alive():
            self._thread.join(0.5)

    def close(self) -> None:
        self._peer.close()
        self.client_socket.close()


class SocketPairTransport(RespRedisTransport):
    """把 `_open_socket` 换成 socketpair 的一端，其余逻辑（connect/AUTH/SELECT）不变。

    为什么只替换建连这一步：这样 `connect()` 的认证、选库、超时设置、
    失败回滚全部走真实代码，而不是绕过它们直接塞一个 socket。
    """

    def __init__(self, server: ScriptedRedisServer, **kwargs) -> None:
        super().__init__(**kwargs)
        self._server = server

    def _open_socket(self) -> socket.socket:
        return self._server.client_socket


def make_connected(response: bytes, **kwargs) -> tuple[RespRedisTransport, ScriptedRedisServer]:
    """建一条连到脚本服务端的 transport（默认无密码、db=0）。"""
    server = ScriptedRedisServer([response])
    transport = SocketPairTransport(server, host="127.0.0.1", timeout=1.0, **kwargs)
    transport.connect()
    return transport, server


def make_parser(chunks: list[bytes]) -> RespParser:
    """构造一个「从 chunks 里逐块取字节」的解析器。

    这是「分片到达」的载体：解析器每次要更多字节时，就吐下一块，
    块边界由测试控制，与真实网络的分片行为等价。
    """
    queue = list(chunks)

    def fill() -> bytes:
        # 用 b"" 触发 EOF：分片用尽还在要数据，说明解析器多读/错位了
        return queue.pop(0) if queue else b""

    return RespParser(fill=fill)


# --------------------------------------------------------------------------- #
# 1. 编码
# --------------------------------------------------------------------------- #
class TestEncoding:
    def test_encode_command_matches_resp_bytes_exactly(self):
        """逐字节断言：`$<len>` 错一位，Redis 就会把参数读错位。"""
        encoded = encode_command(["XADD", "s", "*", "payload", "{}"])

        assert encoded == (b"*5\r\n"
                           b"$4\r\nXADD\r\n"
                           b"$1\r\ns\r\n"
                           b"$1\r\n*\r\n"
                           b"$7\r\npayload\r\n"
                           b"$2\r\n{}\r\n")

    def test_encode_uses_byte_length_for_non_ascii_arguments(self):
        """中文参数：`$<len>` 必须是字节数（4 个汉字 = 12 字节），不是字符数。"""
        encoded = encode_command(["SET", "键", "值"])

        assert encoded == (b"*3\r\n"
                           b"$3\r\nSET\r\n"
                           b"$3\r\n" + "键".encode("utf-8") + b"\r\n"
                           b"$3\r\n" + "值".encode("utf-8") + b"\r\n")
        # 直接断言长度字段与字节数一致（避免上面手写常量写错时两边同时错）
        assert "键".encode("utf-8") == b"\xe9\x94\xae"

    def test_encode_rejects_empty_command(self):
        """空数组不是合法命令：发出去只会让服务端报协议错误。"""
        with pytest.raises(ValueError, match="至少需要一个参数"):
            encode_command([])


# --------------------------------------------------------------------------- #
# 2-9. 解析（单个响应类型）
# --------------------------------------------------------------------------- #
class TestRespParsing:
    def test_simple_string(self):
        """`+OK` 是状态回复，解析成不带 `+` 的 str。"""
        parser = RespParser()
        parser.feed(b"+OK\r\n")

        assert parser.read_reply() == "OK"

    def test_integer(self):
        """`:` 前缀是带符号整数，必须解析成 int 而不是 str。"""
        parser = RespParser()
        parser.feed(b":5\r\n")

        result = parser.read_reply()
        assert result == 5
        assert isinstance(result, int) and not isinstance(result, bool)

    def test_bulk_string(self):
        """`$5` + 正文：正文是值本身，尾部 CRLF 属于协议不进入值。"""
        parser = RespParser()
        parser.feed(b"$5\r\nhello\r\n")

        assert parser.read_reply() == "hello"

    def test_null_bulk_string_is_none(self):
        """`$-1` 是 null（值不存在），跟 `$0`（空字符串）语义完全不同。"""
        parser = RespParser()
        parser.feed(b"$-1\r\n")

        assert parser.read_reply() is None
        # 反向断言：空 bulk string 必须是 ""，不能被当成 None 统一掉
        parser.feed(b"$0\r\n\r\n")
        assert parser.read_reply() == ""

    def test_array(self):
        parser = RespParser()
        parser.feed(b"*2\r\n$3\r\nfoo\r\n$3\r\nbar\r\n")

        assert parser.read_reply() == ["foo", "bar"]

    def test_nested_array(self):
        """XREADGROUP 的真实形状是「数组套数组再套数组」，必须递归解析。

        注意：这里故意在数组里混入整型与嵌套数组，确认递归处理的是**任意**
        RESP 值，而不是只对 bulk string 生效。
        """
        payload = (b"*2\r\n"
                   b"*2\r\n"
                   b"$5\r\nfirst\r\n"
                   b"*3\r\n"
                   b"$5\r\ninner\r\n"
                   b":42\r\n"
                   b"$-1\r\n"
                   b"*0\r\n")
        parser = RespParser()
        parser.feed(payload)

        result = parser.read_reply()
        assert result == [["first", ["inner", 42, None]], []]

    def test_fragmented_delivery_yields_same_result_as_single_chunk(self):
        """**流式解析核心测试**：把同一段响应按字节切碎逐块喂，结果必须一致。

        为什么这样切：每个 CRLF / 每个 bulk string 的中间 / 前缀字节后面
        都断一次，覆盖「行读到一半」「正文读到一半」「尾部 CRLF 还没到」
        三种断点——这三处是手写解析器最常出错的位置。
        """
        full = (b"*1\r\n"
                b"*2\r\n"
                b"$15\r\ntraceops.events\r\n"
                b"*1\r\n"
                b"*2\r\n"
                b"$3\r\n1-0\r\n"
                b"*2\r\n"
                b"$7\r\npayload\r\n"
                b"$2\r\n{}\r\n")

        # 逐字节喂（最细粒度），顺带断言解析器不会因为「只有一个字节」就错判
        bytewise = [full[index:index + 1] for index in range(len(full))]
        fragmented = make_parser(bytewise).read_reply()

        whole = RespParser()
        whole.feed(full)

        assert fragmented == whole.read_reply()
        # 断言内容本身，而不只是「两次结果相等」：两边都错也可能相等。
        # 注意这里的期望形状是 **RESP 原始结构**（列表套列表，字段仍是扁平数组），
        # 转成 Protocol 的 dict 形状是下一层 `_streams_from_raw` 的职责。
        assert fragmented == [[STREAM, [["1-0", ["payload", "{}"]]]]]

    def test_fragmented_delivery_across_crlf_boundary(self):
        """单独钉住「一个 chunk 停在 `\\r`、`\\n` 在下一块」这种断法。

        为什么要单列：很多实现只在 `buf.endswith(b"\\r\\n")` 时认一行，
        这时上一个 chunk 恰好以 `\\r` 结尾就会永久卡在没有 CRLF 的状态。
        """
        # `traceops.events` 是 15 字节；第 2 块末尾的 `\r` 正好是 CRLF 的前半，
        # 后半 `\n` 落在第 3 块 —— 这是「只认 endswith(CRLF)」的实现会卡住的位置。
        chunks = [b"*1\r\n*2\r\n$15\r\ntraceops.", b"events\r",
                  b"\n*1\r\n*2\r\n$3\r\n1-0\r\n",
                  b"*2\r\n$7\r\npayl", b"oad\r\n$2\r\n{", b"}\r\n"]
        parser = make_parser(chunks)

        assert parser.read_reply() == [[STREAM, [["1-0", ["payload", "{}"]]]]]

    def test_error_reply_raises_redis_command_error(self):
        """`-ERR` 是命令失败：必须抛异常，不能当普通字符串往下传。"""
        parser = RespParser()
        parser.feed(b"-ERR unknown command\r\n")

        with pytest.raises(RedisCommandError) as excinfo:
            parser.read_reply()

        # 错误文本要完整保留：运维只看得到这条信息
        assert excinfo.value.message == "ERR unknown command"
        assert "ERR unknown command" in str(excinfo.value)

    def test_error_reply_does_not_poison_the_parser(self):
        """`-ERR` 之后解析器仍可继续用（服务端没关连接）。"""
        parser = RespParser()
        parser.feed(b"-ERR unknown command\r\n")
        with pytest.raises(RedisCommandError):
            parser.read_reply()

        parser.feed(b":1\r\n")
        assert parser.read_reply() == 1

    def test_null_array_means_empty_read(self):
        """`*-1` 是 BLOCK 超时的回包（而不是错误），解析成 None 由上层转空列表。"""
        parser = RespParser()
        parser.feed(b"*-1\r\n")

        assert parser.read_reply() is None

    def test_eof_mid_response_raises_connection_error(self):
        """对端只发半个值就断开：必须抛连接错误，绝不能返回半个值。"""
        parser = make_parser([b"*2\r\n$3\r\nfoo\r\n"])  # 声明 2 个元素只给了 1 个

        with pytest.raises(RedisConnectionError, match="对端关闭"):
            parser.read_reply()

    def test_unknown_prefix_raises_protocol_error(self):
        """前缀不认识说明已经错位：解析器不能猜，必须让连接失效。"""
        parser = RespParser()
        parser.feed(b"?broken\r\n")

        with pytest.raises(RedisProtocolError, match="前缀"):
            parser.read_reply()


# --------------------------------------------------------------------------- #
# 10-12. 三条命令的真实回包 → Protocol 形状
# --------------------------------------------------------------------------- #
class TestCommandShapes:
    def test_xreadgroup_flattens_fields_into_dict(self):
        """真实的 XREADGROUP 回包：entry 的字段是 `[f, v, f, v, ...]`，必须转 dict。

        断言完整形状，而不只是「有数据」：上层 `RedisStreamEventSource.poll`
        就是按 `[(stream_key, [(entry_id, fields), ...])]` 迭代的。
        """
        response = (b"*1\r\n"
                    b"*2\r\n"
                    b"$15\r\ntraceops.events\r\n"
                    b"*1\r\n"
                    b"*2\r\n"
                    b"$3\r\n1-0\r\n"
                    b"*2\r\n"
                    b"$7\r\npayload\r\n"
                    b"$2\r\n{}\r\n")
        transport, server = make_connected(response)
        try:
            result = transport.xreadgroup(GROUP, CONSUMER, STREAM, 10, 1000)
        finally:
            transport.close()
            server.close()

        assert result == [(STREAM, [("1-0", {"payload": "{}"})])]
        # 发出去的命令也必须对：GROUP/COUNT/BLOCK/STREAMS 顺序错一个就是别的语义
        assert server.commands[0] == [
            "XREADGROUP", "GROUP", GROUP, CONSUMER,
            "COUNT", "10", "BLOCK", "1000", "STREAMS", STREAM, ">",
        ]

    def test_xreadgroup_block_timeout_returns_empty_list(self):
        """`BLOCK` 超时 Redis 回 `*-1`：上层契约是空列表，不是 None。"""
        transport, server = make_connected(b"*-1\r\n")
        try:
            result = transport.xreadgroup(GROUP, CONSUMER, STREAM, 10, 0)
        finally:
            transport.close()
            server.close()

        assert result == []
        assert isinstance(result, list)

    def test_xreadgroup_extra_fields_are_all_kept(self):
        """entry 带多个字段时不能只取 payload：上层要能审计其他字段。

        `1700000000000` 是 13 字节，所以 `$13`。手写这些长度前缀很容易错，
        测试本身就是「编码规则」的第二次独立检查。
        """
        response = (b"*1\r\n"
                    b"*2\r\n"
                    b"$15\r\ntraceops.events\r\n"
                    b"*1\r\n"
                    b"*2\r\n"
                    b"$13\r\n1700000000000\r\n"
                    b"*4\r\n"
                    b"$7\r\npayload\r\n"
                    b"$2\r\n{}\r\n"
                    b"$2\r\nts\r\n"
                    b"$13\r\n1769783160000\r\n")
        transport, server = make_connected(response)
        try:
            result = transport.xreadgroup(GROUP, CONSUMER, STREAM, 10, 1000)
        finally:
            transport.close()
            server.close()

        assert result == [(STREAM, [("1700000000000", {
            "payload": "{}", "ts": "1769783160000"})])]

    def test_xack_returns_integer(self):
        """`XACK` 回 `:1`：必须是 int，上层用它区分「不在 PEL」和「确认成功」。"""
        transport, server = make_connected(b":1\r\n")
        try:
            result = transport.xack(STREAM, GROUP, "1-0")
        finally:
            transport.close()
            server.close()

        assert result == 1
        assert isinstance(result, int)
        assert server.commands[0] == ["XACK", STREAM, GROUP, "1-0"]

    def test_xack_zero_means_entry_not_in_pel(self):
        """0 是合法返回值（id 已不在 PEL），不能被当成「没结果」吃掉。"""
        transport, server = make_connected(b":0\r\n")
        try:
            assert transport.xack(STREAM, GROUP, "9-9") == 0
        finally:
            transport.close()
            server.close()

    def test_xautoclaim_returns_three_element_shape(self):
        """XAUTOCLAIM 的回包是三元素：游标、entries、被删除的 id。

        `redis_source.py` 的 `_extract_cursor` 只在 `len == 3` 时认游标，
        少一个元素会让 PEL 游标永远停在 0-0（尾部饿死），所以形状必须被钉住。
        """
        response = (b"*3\r\n"
                    b"$3\r\n0-0\r\n"
                    b"*1\r\n"
                    b"*2\r\n"
                    b"$3\r\n1-0\r\n"
                    b"*2\r\n"
                    b"$7\r\npayload\r\n"
                    b"$2\r\n{}\r\n"
                    b"*1\r\n"
                    b"$3\r\n2-0\r\n")
        transport, server = make_connected(response)
        try:
            result = transport.xautoclaim(STREAM, GROUP, CONSUMER, 60000, "0-0", 10)
        finally:
            transport.close()
            server.close()

        assert result == ["0-0", [("1-0", {"payload": "{}"})], ["2-0"]]
        assert len(result) == 3
        assert isinstance(result[0], str)  # 游标是 str，_extract_cursor 只认 str
        assert server.commands[0] == [
            "XAUTOCLAIM", STREAM, GROUP, CONSUMER, "60000", "0-0", "COUNT", "10",
        ]

    def test_xautoclaim_with_deleted_entries_does_not_fake_them_into_entries(self):
        """被 XDEL 但仍在 PEL 的 id 只能出现在第三项，不能混进正常 entries。"""
        response = (b"*3\r\n"
                    b"$3\r\n5-0\r\n"
                    b"*0\r\n"
                    b"*2\r\n"
                    b"$3\r\n7-0\r\n"
                    b"$3\r\n8-0\r\n")
        transport, server = make_connected(response)
        try:
            cursor, entries, deleted = transport.xautoclaim(
                STREAM, GROUP, CONSUMER, 0, "0-0", 1)
        finally:
            transport.close()
            server.close()

        assert cursor == "5-0"
        assert entries == []          # 没有可认领的 entry
        assert deleted == ["7-0", "8-0"]


# --------------------------------------------------------------------------- #
# 连接管理 / 错误处理
# --------------------------------------------------------------------------- #
class TestConnectionHandling:
    def test_connect_to_closed_port_raises_redis_connection_error(self):
        """连一个没人监听的端口：必须是 RedisConnectionError（而不是裸 OSError）。

        为什么要单独的错误类型：上层若用 `except OSError` 捕获，会把
        「Redis 挂了」和「本地文件问题」混在一起。
        """
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        finally:
            # 立刻释放端口，保证接下来连它必然被拒
            probe.close()

        transport = RespRedisTransport(host="127.0.0.1", port=closed_port, timeout=1.0)

        with pytest.raises(RedisConnectionError, match="无法连接"):
            transport.connect()
        # 失败后不能留下半个连接状态
        assert transport.connected is False

    def test_calling_methods_without_connect_raises_connection_error(self):
        """未连接就调用：抛 RedisConnectionError，而不是 AttributeError。

        为什么这一点重要：worker 在真实部署里如果忘了 connect，用户看到
        `'NoneType' object has no attribute 'sendall'` 是查不出根因的。
        """
        transport = RespRedisTransport(host="127.0.0.1", port=6379, timeout=1.0)

        with pytest.raises(RedisConnectionError, match="尚未连接"):
            transport.xreadgroup(GROUP, CONSUMER, STREAM, 10, 1000)
        with pytest.raises(RedisConnectionError, match="尚未连接"):
            transport.xack(STREAM, GROUP, "1-0")
        with pytest.raises(RedisConnectionError, match="尚未连接"):
            transport.xautoclaim(STREAM, GROUP, CONSUMER, 1000, "0-0", 10)

    def test_command_error_keeps_connection_usable(self):
        """`-ERR` 是命令失败而不是连接失败：同一条连接必须能继续发命令。"""
        server = ScriptedRedisServer([b"-ERR unknown command\r\n", b":1\r\n"])
        transport = SocketPairTransport(server, host="127.0.0.1", timeout=1.0)
        transport.connect()
        try:
            with pytest.raises(RedisCommandError, match="unknown command"):
                transport.xack(STREAM, GROUP, "1-0")
            assert transport.connected is True
            assert transport.xack(STREAM, GROUP, "1-0") == 1
        finally:
            transport.close()
            server.close()

    def test_connection_dropped_before_reply_raises_and_invalidates_connection(self):
        """服务端不回话就断开：抛 RedisConnectionError，并把连接标记为不可用。

        为什么要标记不可用：读到 EOF 时可能已经收到半个回包，继续用这条连接
        会让下一条命令的结果错位（串包），比直接失败危险得多。
        """
        # 先发一个「声明 2 个元素、只给了 1 个」的半个数组，然后由服务端自己断开。
        # hangup_after_script=True 保证断开来自对端（真实的 EOF），而不是测试线程
        # 直接 close 客户端 socket（那会变成 "Bad file descriptor"，语义就错了）。
        server = ScriptedRedisServer([b"*2\r\n$3\r\nfoo\r\n"], hangup_after_script=True)
        transport = SocketPairTransport(server, host="127.0.0.1", timeout=1.0)
        transport.connect()
        try:
            # 必须匹配 EOF 文案：否则「socket 超时」也会被这个断言接受，
            # 测试就变成「等了 1 秒然后通过」的假绿
            with pytest.raises(RedisConnectionError, match="对端关闭"):
                transport.xack(STREAM, GROUP, "1-0")
            # 读完 EOF 后这条连接已不可信：必须被标记为不可用，下一步只能重建
            assert transport.connected is False
        finally:
            transport.close()
            server.close()

    def test_server_hangup_without_any_reply_raises_connection_error(self):
        """一条响应都没有就对端关闭：也是连接错误，不能无限等下去。"""
        server = ScriptedRedisServer([], hangup_after_script=True)
        transport = SocketPairTransport(server, host="127.0.0.1", timeout=1.0)
        transport.connect()
        try:
            # 必须匹配 EOF 文案：否则「socket 超时」也会被这个断言接受，
            # 测试就变成「等了 1 秒然后通过」的假绿
            with pytest.raises(RedisConnectionError, match="对端关闭"):
                transport.xack(STREAM, GROUP, "1-0")
        finally:
            transport.close()
            server.close()

    def test_close_is_idempotent(self):
        """close 被重复调用（__exit__ 与 finally）不能抛异常。"""
        server = ScriptedRedisServer([b"+OK\r\n"])
        transport = SocketPairTransport(server, host="127.0.0.1", timeout=1.0)
        transport.connect()
        transport.close()
        transport.close()  # 第二次必须是 no-op
        server.close()

        assert transport.connected is False

    def test_context_manager_closes_connection(self):
        """`with` 退出必须关连接；否则 worker 重启会泄漏 socket。"""
        server = ScriptedRedisServer([b":1\r\n"])
        transport = SocketPairTransport(server, host="127.0.0.1", timeout=1.0)

        with transport as entered:
            assert entered is transport
            assert entered.connected is True
            assert entered.xack(STREAM, GROUP, "1-0") == 1

        assert transport.connected is False

    def test_connect_sends_auth_and_select_when_configured(self):
        """配置了 password/db 时必须先 AUTH 再 SELECT，顺序不能反。

        顺序错了在真实 Redis 上的表现是 `NOAUTH` 或「选错库」，
        在没有 Redis 的环境里至少能验证发出去的命令序列是对的。
        """
        # 三个回包：AUTH 与 SELECT 都回 +OK，XACK 回 :1（顺序即服务端应答顺序）
        server = ScriptedRedisServer([b"+OK\r\n", b"+OK\r\n", b":1\r\n"])
        transport = SocketPairTransport(server, host="127.0.0.1", timeout=1.0,
                                        password="s3cret", db=3)
        transport.connect()
        try:
            transport.xack(STREAM, GROUP, "1-0")
            server.wait_until_idle()
            assert server.commands == [
                ["AUTH", "s3cret"],
                ["SELECT", "3"],
                ["XACK", STREAM, GROUP, "1-0"],
            ]
        finally:
            transport.close()
            server.close()

    def test_failed_auth_rolls_back_connection(self):
        """AUTH 被拒（`-ERR invalid password`）时 connect 必须失败并关掉连接。

        为什么不能在半配置状态继续跑：后续命令会打到一个未认证的实例上，
        错误会延后、且表现成「没消息」，排查成本极高。
        """
        server = ScriptedRedisServer([b"-ERR invalid password\r\n"])
        transport = SocketPairTransport(server, host="127.0.0.1", timeout=1.0,
                                        password="wrong")
        try:
            with pytest.raises(RedisCommandError, match="invalid password"):
                transport.connect()
            assert transport.connected is False
        finally:
            transport.close()
            server.close()

    def test_bad_entry_shape_raises_protocol_error(self):
        """字段列表长度为奇数说明读取已错位，不能硬凑成 dict 交给上层。"""
        response = (b"*1\r\n"
                    b"*2\r\n"
                    b"$15\r\ntraceops.events\r\n"
                    b"*1\r\n"
                    b"*2\r\n"
                    b"$3\r\n1-0\r\n"
                    b"*1\r\n"
                    b"$7\r\npayload\r\n")
        transport, server = make_connected(response, )
        try:
            with pytest.raises(RedisProtocolError, match="奇数"):
                transport.xreadgroup(GROUP, CONSUMER, STREAM, 10, 1000)
        finally:
            transport.close()
            server.close()

    def test_rejects_invalid_constructor_arguments(self):
        """明显非法的配置在构造期就失败，不让它变成运行期的 `-ERR`。"""
        with pytest.raises(ValueError, match="host"):
            RespRedisTransport(host="")
        with pytest.raises(ValueError, match="port"):
            RespRedisTransport(port=0)
        with pytest.raises(ValueError, match="timeout"):
            RespRedisTransport(timeout=0)


# --------------------------------------------------------------------------- #
# 与上层 RedisStreamEventSource 的端到端衔接
# --------------------------------------------------------------------------- #
class TestIntegrationWithEventSource:
    def test_event_source_poll_works_over_real_transport(self):
        """真实证据：`RedisStreamEventSource` 拿到 `RespRedisTransport` 后能跑通。

        这里不是测 RedisStreamEventSource 的业务逻辑（它有自己的测试），
        而是验证「我们的返回形状确实能喂进它」——形状差一个元素，
        这条用例就会在 `_extract_cursor` / `_extract_entries` 处暴露。
        """
        claim = (b"*3\r\n" b"$3\r\n0-0\r\n" b"*0\r\n" b"*0\r\n")
        read = (b"*1\r\n"
                b"*2\r\n"
                b"$15\r\ntraceops.events\r\n"
                b"*1\r\n"
                b"*2\r\n"
                b"$3\r\n1-0\r\n"
                b"*2\r\n"
                b"$7\r\npayload\r\n"
                b"$2\r\n{}\r\n")
        # `+OK` 是 XGROUP CREATE 的回执：poll 现在会先确保消费者组存在
        # （全新部署必需 —— 否则真实 Redis 会报 NOGROUP）。
        ok_reply = b"+OK\r\n"
        server = ScriptedRedisServer([ok_reply, claim, read])
        transport = SocketPairTransport(server, host="127.0.0.1", timeout=1.0)
        transport.connect()
        try:
            source = RedisStreamEventSource(transport, stream=STREAM, group=GROUP,
                                            consumer=CONSUMER, block_ms=100,
                                            min_idle_ms=60_000)
            messages = source.poll(limit=10)
        finally:
            transport.close()
            server.close()

        assert messages == [{"id": "1-0", "payload": {}}]


# --------------------------------------------------------------------------- #
# 未经真实 Redis 验证的部分（诚实清单）
# --------------------------------------------------------------------------- #
# 以下行为是「按 RESP/Streams 文档实现」，本文件**无法**验证（需要真实 Redis）：
#
# 1. `XAUTOCLAIM` 的返回值：这里按文档用 `[cursor, entries, deleted]` 三元素。
#    Redis 6.2 引入该命令时是两元素（cursor + entries），6.2.x 之后才有第三项；
#    真机上如果版本更老，多出来的第三项会缺失，实现里已按「缺则空列表」降级，
#    但这条降级路径只在假服务端上跑过。
# 2. `BLOCK` 的真实语义：`BLOCK 0` 表示无限阻塞、`BLOCK <ms>` 到点回 `*-1`。
#    测试只验证发送的参数与 `*-1` 的处理，没有验证真实等待时长。
# 3. socket 超时与 `BLOCK` 的配合：真实 Redis 在阻塞期间不发任何字节，
#    实现把 socket 超时放宽到 `timeout + block_ms`，这个是推导出来的，
#    没有在真实网络环境里压过（尤其是 `BLOCK 0` 把 timeout 设成 None 的行为）。
# 4. `AUTH`/`SELECT` 的失败语义（`-WRONGPASS`/`-NOAUTH`/`-ERR invalid DB index`）：
#    只用了自造的 `-ERR invalid password`，真实错误码文本没对过。
# 5. 大 payload（数 MB 的 bulk string）与 `_RECV_SIZE=65536` 的多次 recv：
#    分片逻辑在字节级被测过，但没有在真实网络吞吐下验证。
# 6. Redis 返回 `MAXLEN` 截断、`NOGROUP` 等场景：`NOGROUP` 会被当作
#    `RedisCommandError` 抛出（连接保持），但没有真实实例确认错误文本前缀。
