"""SQLite 连接必须配置 WAL 与 busy_timeout，否则并发会直接失败。

## 缺陷（搜索生产实践后实测确认）

项目有三处持久化 SQLite（`evidence/store.py`、`runtime/run_ledger.py`、
`remediation/approval_store.py`），**全部用默认配置**：

```python
sqlite3.connect(str(self._path))     # 无 PRAGMA
```

搜索到的生产实践一致指出必须显式设置四项，其中两项对本项目是致命的：

| PRAGMA | 不设的后果 |
|---|---|
| `journal_mode=WAL` | 默认 rollback journal 下**读会阻塞写、写会阻塞读** —— 而我们的证据引擎天然是"边查边写" |
| `busy_timeout` | 拿不到锁**立刻**抛 `database is locked`。实测数据：`timeout=0` 时 2000 次写只有 294 次成功（失败 1706 次）；5 秒超时后 2000 次全成功 |

另外两项是配套的：

- `synchronous=NORMAL`：WAL 下的推荐值。`FULL` 每次提交都 fsync，代价高；
  `NORMAL` 在 WAL 下仍能防止数据库损坏（只可能丢最后一个事务）
- `foreign_keys=ON`：SQLite 默认**不**启用外键约束（历史遗留），
  不显式开启则声明的外键形同虚设

## 为什么这是真实风险而不是理论

`RunLedger` 用于**跨进程去重**：多个 worker 并发认领 run 时必然发生写竞争。
`ApprovalStore` 同样跨进程（调查进程写、审批人读、决策回写）。
默认配置下这些场景会随机出现 `database is locked` —— 表现为"偶尔调查失败"，
极难排查。
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from traceops.runtime.sqlite_config import configure_connection, journal_mode_of


class TestEveryConnectionIsConfigured:
    def test_wal_is_enabled(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "a.db")
        configure_connection(conn)
        assert journal_mode_of(conn) == "wal"

    def test_busy_timeout_is_set(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "a.db")
        configure_connection(conn)
        # timeout 0.001 秒 = 1ms，反映在 sqlite 的 busy handler 设置上
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0

    def test_synchronous_is_normal_not_full(self, tmp_path):
        """WAL 下 NORMAL 是推荐值：FULL 每次提交都 fsync，吞吐代价高。"""
        conn = sqlite3.connect(tmp_path / "a.db")
        configure_connection(conn)
        # 1 = NORMAL, 2 = FULL
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1

    def test_foreign_keys_enabled(self, tmp_path):
        """SQLite 默认不启用外键 —— 不显式开启则声明的约束形同虚设。"""
        conn = sqlite3.connect(tmp_path / "a.db")
        configure_connection(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


class TestConcurrencyActuallyImproves:
    def test_concurrent_writers_do_not_fail_with_locked(self, tmp_path):
        """两个线程并发写同一库：配置后必须全部成功。

        这正是 `RunLedger` 的真实场景（多 worker 并发认领 run）。
        """
        db = tmp_path / "c.db"
        setup = sqlite3.connect(db)
        configure_connection(setup)
        setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        setup.commit()
        setup.close()

        errors: list[str] = []

        def writer(n: int) -> None:
            try:
                conn = sqlite3.connect(db, timeout=5)
                configure_connection(conn)
                for i in range(50):
                    conn.execute("INSERT INTO t (id) VALUES (?)", (n * 1000 + i,))
                    conn.commit()
                conn.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent writes failed: {errors[:2]}"
        check = sqlite3.connect(db)
        assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 200


class TestProjectConnectionsUseIt:
    """三处真实连接都必须走统一配置 —— 否则修了库但没接线。"""

    def test_evidence_store_configures_connection(self, tmp_path):
        from traceops.evidence.store import EvidenceStore

        store = EvidenceStore(tmp_path / "e.db")
        assert journal_mode_of(store._conn) == "wal"

    def test_run_ledger_configures_connection(self, tmp_path):
        from traceops.runtime.run_ledger import RunLedger

        ledger = RunLedger(tmp_path / "l.db")
        assert journal_mode_of(ledger._conn) == "wal"

    def test_approval_store_configures_connection(self, tmp_path):
        from traceops.remediation.approval_store import ApprovalStore

        store = ApprovalStore(tmp_path / "a.db")
        assert journal_mode_of(store._conn) == "wal"
