"""协作式停机：收到 SIGTERM 后**把手头的活做完**再退出。

## 为什么需要（对照容器化实践）

`cli/worker.py` 原先只捕获 `KeyboardInterrupt`（SIGINT，交互式 Ctrl-C），
**没有 SIGTERM 处理**。而在容器里 SIGTERM 是标准停止信号，没有处理器时
进程被硬杀，后果是数据层面的：

- 正在处理的调查**中途消失**，报告永远不产出
- 消息已投递给该 consumer 但未 ACK → 留在 PEL 成为 pending
- 要等 `min_idle_ms`（默认 60 秒）后才能被 XAUTOCLAIM 认领回去
- 那 60 秒里这条事故毫无进展，而外部监控看起来一切正常

Kubernetes 的 `terminationGracePeriodSeconds`（默认 10 秒）本意就是
"让进程把手头的事做完"，而我们主动放弃了这个机会。

## 设计：协作式而不是抢占式

信号处理器**只**设置一个标志，绝不中断正在执行的逻辑 ——
在信号处理器里做重活（关连接、写库）是危险的：它可能在任意指令处打断，
留在半写的状态里。

主循环在**批次之间**检查标志。这样：

- 当前批次的每条消息都会被 ACK（不产生新的 pending）
- 退出点确定，没有"处理到一半"的中间态

## 为什么记录信号名

停机日志若只说"stopped"，运维分不清是 SIGTERM（部署）还是 SIGINT（人按的）。
两者后续动作完全不同。
"""
from __future__ import annotations

import signal
from contextlib import contextmanager
from typing import Any, Iterator


class StopSignal:
    """协作式停机标志。

    **构造即安装信号处理器**（SIGINT/SIGTERM）—— 这不是普通构造。
    想撤销用 `with controller.installed():`（退出 with 时恢复旧处理器）。

    线程安全：信号处理器在主线程被调用，而检查点可能在别处；
    用 `threading.Event` 而不是裸 bool 以保证可见性。
    """

    def __init__(self) -> None:
        import threading

        self._event = threading.Event()
        self.signal_name: str | None = None
        self._previous: dict[int, Any] = {}

    @property
    def stopping(self) -> bool:
        return self._event.is_set()

    def request_stop(self, *, signal_name: str | None = None) -> None:
        """请求停机。幂等 —— 重复信号（如用户连按两次 Ctrl-C）是正常的。"""
        if signal_name and not self.signal_name:
            self.signal_name = signal_name
        self._event.set()

    def reset(self) -> None:
        """清空标志（测试与"暂停后恢复"场景用）。"""
        self._event.clear()
        self.signal_name = None

    @contextmanager
    def installed(self, signals: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT)) -> Iterator["StopSignal"]:
        """安装处理器，退出时**恢复原处理器**。

        必须恢复：测试里多次安装会让前一次的处理器泄漏，
        而生产里嵌套安装会把 SIGINT 的默认行为改掉且无法还原。
        """
        def _handler(signum, frame):  # noqa: ANN001 - signal handler signature
            name = signal.Signals(signum).name
            self.request_stop(signal_name=name)

        self._previous = {}
        for sig in signals:
            try:
                self._previous[sig] = signal.getsignal(sig)
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # 非主线程无法安装处理器；此时静默跳过而不是让 worker 启动失败 ——
                # 没有信号处理仍能工作，只是弱一些。
                pass
        try:
            yield self
        finally:
            for sig, previous in self._previous.items():
                try:
                    signal.signal(sig, previous)
                except (ValueError, OSError):
                    pass
            self._previous = {}


