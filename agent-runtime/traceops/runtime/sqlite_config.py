"""SQLite 连接的统一配置。

## 为什么必须有这个模块

项目有三处持久化 SQLite，历史上**全部用默认配置**（`sqlite3.connect(path)`），
而默认配置在并发下会直接失败。搜索到的生产实践与本项目场景一一对应：

| PRAGMA | 不设的后果 | 本项目的触发场景 |
|---|---|---|
| `journal_mode=WAL` | 默认 rollback journal: 读阻塞写、写阻塞读 | 证据引擎天然"边查边写" |
| `busy_timeout` | 拿不到锁**立刻**抛 `database is locked` | `RunLedger` 多 worker 并发认领 |
| `synchronous=NORMAL` | `FULL` 每次提交 fsync，吞吐代价高 | 每次工具调用都落证据 |
| `foreign_keys=ON` | SQLite **默认不启用**外键 | 已声明的约束形同虚设 |

`busy_timeout` 的实测差距很极端：某基准里 `timeout=0` 时 2000 次写只有 294 次
成功（失败 1706 次）；设为 5 秒后 2000 次全成功。这个差别就是"偶尔调查失败"
与"稳定运行"的差别，而前者极难排查。

## 为什么集中在模块里而不是各调用点各写一遍

四处连接分散在三个模块。把 PRAGMA 写在每一处，迟早会漏一处（而且新增
第四个库时必然漏）。集中成一个函数，调用点只做一次调用。
"""
from __future__ import annotations

import sqlite3

#: 默认 busy 超时（毫秒）。
#:
#: 5000ms 的理由：取得与生产实践一致的量级 —— 足够跨过一次典型的事务提交，
#: 又不至于在库真的被锁死时让调用方无限等待。
DEFAULT_BUSY_TIMEOUT_MS = 5000


def configure_connection(
    conn: sqlite3.Connection, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> sqlite3.Connection:
    """对连接应用生产配置。返回同一个连接（便于链式调用）。

    只使用 `PRAGMA`，不改变 schema，也不开启任何需要额外依赖的特性。
    """
    if busy_timeout_ms > 0:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")

    # WAL：读不阻塞写。内存库（":memory:"）不支持 WAL，此时 SQLite 会保持
    # 原模式而不报错 —— 所以这里不需要额外判断，直接设即可。
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # 只读挂载或不支持 WAL 的文件系统上这一步会失败；
        # 让它失败而不是让整个连接不可用 —— 没有 WAL 仍能工作，只是并发差些。
        pass

    # WAL 下的推荐同步级别：FULL 会每次提交 fsync，代价高；
    # NORMAL 在 WAL 下仍能防止数据库损坏（只可能丢最后一个事务）。
    conn.execute("PRAGMA synchronous = NORMAL")
    # SQLite 默认**不**启用外键约束（历史兼容性），必须显式开启。
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def journal_mode_of(conn: sqlite3.Connection) -> str:
    """当前 journal 模式（小写）。诊断与测试用。"""
    row = conn.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).lower() if row else ""
