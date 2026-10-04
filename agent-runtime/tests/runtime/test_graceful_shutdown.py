"""worker 必须能**优雅停机**：收到 SIGTERM 后处理完手头消息再退出。

## 缺陷（对照容器化实践发现）

`cli/worker.py` 只捕获 `KeyboardInterrupt`（SIGINT，交互式场景），
**没有 SIGTERM 处理**。而在容器里 SIGTERM 是标准停止信号 ——
没有它，进程被硬杀：

- 正在处理的调查**中途消失**，报告永远不产出
- 消息已投递给该 consumer 但未 ACK → 留在 PEL 里成为 pending
  直到 `min_idle_ms`（默认 60s）之后才能被 XAUTOCLAIM 认领回去
- 那 60 秒里这条事故**没有任何进展**，而外部看一切正常

Kubernetes 默认给 10 秒宽限期（`terminationGracePeriodSeconds`），
设计意图正是"让进程把手头的事做完" —— 而我们主动放弃了这个机会。

## 正确行为

- SIGTERM/SIGINT 设停标志，**不立即退出**
- 当前这轮 `poll_once` 跑完（消息被 ACK）再退出
- 退出时打印原因，便于排障

## 边界

不做 `PreStop` 钩子、不做 readiness 端点：那是部署层的事。
本模块只保证"收到停止信号后行为正确"。
"""
from __future__ import annotations

import signal
import threading

import pytest


class TestStopSignalIsCooperative:
    def test_stop_flag_can_be_set_and_checked(self):
        from traceops.runtime.shutdown import StopSignal

        controller = StopSignal()
        assert not controller.stopping
        controller.request_stop()
        assert controller.stopping

    def test_request_stop_records_the_signal_name(self):
        """停机原因要有记录 —— 否则日志里只剩"停了"，不知为何。"""
        from traceops.runtime.shutdown import StopSignal

        controller = StopSignal()
        controller.request_stop(signal_name="SIGTERM")
        assert controller.signal_name == "SIGTERM"

    def test_second_signal_is_idempotent(self):
        controller_source = __import__(
            "traceops.runtime.shutdown", fromlist=["StopSignal"]).StopSignal
        controller = controller_source()
        controller.request_stop(signal_name="SIGTERM")
        controller.request_stop(signal_name="SIGTERM")
        assert controller.stopping

    def test_install_restores_previous_handlers(self):
        """安装信号处理器要能卸载 —— 测试与嵌套使用都需要。"""
        from traceops.runtime.shutdown import StopSignal

        controller = StopSignal()
        with controller.installed():
            pass  # 进出不抛异常即证明可安装/可恢复

    def test_handler_is_actually_installed(self):
        from traceops.runtime.shutdown import StopSignal

        controller = StopSignal()
        original = signal.getsignal(signal.SIGTERM)
        try:
            with controller.installed():
                assert signal.getsignal(signal.SIGTERM) != original, (
                    "SIGTERM handler was not replaced, so the container stop "
                    "signal would still kill the process outright")
        finally:
            assert signal.getsignal(signal.SIGTERM) == original


class TestWorkerHonoursStopSignal:
    def test_run_loop_exits_after_current_batch(self):
        """停机标志生效时，主循环应在**批次之间**退出，而不是中途打断。

        这条用 run_loop 的行为验证：若处理器里直接抛异常，当前批次的消息
        就会留在 PEL 变成 pending —— 那是真实的 60 秒停滞。
        """
        from traceops.cli.worker import _run_until_stopped
        from traceops.runtime.shutdown import StopSignal
        from traceops.runtime.worker import InMemoryEventSink, InMemoryEventSource

        source, sink = InMemoryEventSource(), InMemoryEventSink()
        from traceops.runtime.worker import InvestigationWorker

        worker = InvestigationWorker(
            source=source, sink=sink,
            derive_request=lambda m: None, build_context=lambda r: None)
        controller = StopSignal()
        controller.request_stop(signal_name="SIGTERM")

        # 已经请求停机 → 不再进入轮询，直接返回（批次级检查生效）
        polls = _run_until_stopped(worker, controller, max_iterations=3)
        assert polls == 0, f"should not poll after stop was requested, polled {polls}"
