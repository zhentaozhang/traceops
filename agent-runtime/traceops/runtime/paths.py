"""默认落盘位置。

## 为什么必须有这个模块

worker 的四个存储（证据库 / run 账本 / 审批库 / 事故记忆）曾经**缺省都是
`:memory:`**。后果不是"性能差一点"，而是：照文档起 worker，进程重启后
所有证据消失，**而且没有任何日志**。运维唯一能观察到的现象是"上次调查的
证据 ID 打不开了"，而那时上下文已经没了。

缺省值属于产品行为的一部分，所以它必须指向一个**真的会留下来的地方**，
而"我这次就想用临时库"必须是一个显式选择（`--ephemeral`）。
"""
from __future__ import annotations

import os
from pathlib import Path

#: 环境变量：把所有默认落盘位置整体挪走（容器里挂卷时用）。
STATE_DIR_ENV = "TRACEOPS_STATE_DIR"


def default_state_dir() -> Path:
    """默认状态目录：`$TRACEOPS_STATE_DIR`，否则 `~/.local/state/traceops`。

    不用 `tempfile.gettempdir()`：临时目录里的文件会被系统清理，
    而"证据 ID 指向一个已经消失的文件"是最难排查的一类问题。
    """
    raw = os.environ.get(STATE_DIR_ENV)
    base = Path(raw).expanduser() if raw else Path.home() / ".local" / "state" / "traceops"
    return base


def default_db_path(filename: str) -> Path:
    """状态目录下的一个 SQLite 文件路径（父目录已创建）。"""
    if not filename or "/" in filename:
        raise ValueError(f"expected a plain filename, got {filename!r}")
    path = default_state_dir() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
