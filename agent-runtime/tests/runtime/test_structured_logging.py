"""核心运行时必须有**结构化日志**，否则生产故障不可排障。

## 缺陷（搜索日志实践后实测确认）

`traceops.runtime.worker` / `orchestration.graph` / `remediation.executor`
三个核心模块里，`logging` 与 `print` 的计数**都是 0**。

后果：
- 调查失败时没有任何记录 —— 只能从外部看到"事件没回"，不知道断在哪
- 一个批次里哪条消息失败、为什么失败，事后完全无法还原
- 死信里只有 `reason` 字符串，没有上下文（哪个 run、哪个节点、第几次尝试）

## 为什么用标准库 `logging` 而不是 structlog

本项目依赖策略是标准库 + pydantic + jsonschema。`structlog` 能提供更好的
结构化输出，但它带来的收益（主要是漂亮的渲染与绑定上下文）可以由
标准库的 `extra=` 参数覆盖。为日志引入第三个依赖不划算。

## 三条约束

1. **库代码不配置 handler**：只在入口（CLI）配置一次 ——
   库若自己配置，会把宿主程序的日志设置覆盖掉
2. **结构化字段走 `extra=`**，不要拼进 message 字符串 ——
   拼进去就再也无法按字段过滤/聚合
3. **默认静默**：库代码不能因为没配置 handler 就往 stderr 打东西
"""
from __future__ import annotations

import logging

import pytest


class TestLibraryLoggingHygiene:
    def test_core_modules_get_a_logger(self):
        """三个核心模块必须各有一个 logger。"""
        from traceops.orchestration import graph
        from traceops.remediation import executor
        from traceops.runtime import worker

        for module in (worker, graph, executor):
            assert hasattr(module, "log"), (
                f"{module.__name__} has no logger; failures there are invisible")

    def test_library_does_not_configure_handlers(self):
        """库代码不得添加 handler —— 那会覆盖宿主的日志配置。"""
        from traceops.runtime import worker

        source = open(worker.__file__, encoding="utf-8").read()
        assert "basicConfig" not in source, (
            "library code must not call basicConfig; configure once at the entry point")
        assert "addHandler" not in source, (
            "library code must not add handlers")

    def test_logger_names_follow_module_hierarchy(self):
        """logger 名应为 __name__ —— 便于按包名分级过滤。"""
        from traceops.runtime import worker

        assert worker.log.name == worker.__name__


class TestStructuredFieldsAreUsable:
    def test_records_carry_extra_fields(self, caplog):
        """走 extra= 而不是拼字符串：否则无法按字段过滤。

        用一条真实的失败路径验证：坏消息进死信时应当带上可检索的上下文。
        """
        from traceops.runtime.worker import InvestigationWorker, InMemoryEventSink, InMemoryEventSource

        def explode(_message):
            raise ValueError("bad payload")

        worker = InvestigationWorker(
            source=InMemoryEventSource(), sink=InMemoryEventSink(),
            derive_request=explode, build_context=lambda r: None)

        with caplog.at_level(logging.WARNING, logger="traceops.runtime.worker"):
            worker.source.push({"id": "m-1", "payload": {}})
            worker.poll_once()

        assert caplog.records, "a failed message must leave a log record"
        # 结构化字段，而不是纯文本
        assert any(
            getattr(r, "message_id", None) == "m-1" for r in caplog.records
        ), f"no structured message_id field: {[r.__dict__ for r in caplog.records]}"


class TestExtraFieldsAreActuallyRendered:
    """字段挂在 LogRecord 上但**不渲染**，等于字段存在但不可见。

    这是我实测发现的问题：formatter 只输出 message 时，
    `message_id` / `error_type` 虽然在 record 上，
    但运维从日志里一个字也看不到 —— 排查时仍只能看到一句纯文本。
    """

    def test_formatter_renders_extra_fields(self):
        import logging

        from traceops.cli.worker import _StructuredFormatter

        record = logging.LogRecord("t", logging.WARNING, "f", 1,
                                   "boom", (), None)
        record.message_id = "m-9"
        record.error_type = "ValueError"
        rendered = _StructuredFormatter("%(message)s").format(record)
        assert "message_id=m-9" in rendered
        assert "error_type=ValueError" in rendered

    def test_formatter_does_not_duplicate_builtin_fields(self):
        import logging

        from traceops.cli.worker import _StructuredFormatter

        record = logging.LogRecord("t", logging.WARNING, "f", 1,
                                   "boom", (), None)
        rendered = _StructuredFormatter("%(message)s").format(record)
        assert "message=boom" not in rendered, (
            "message is already the formatted output; echoing it again is noise")
