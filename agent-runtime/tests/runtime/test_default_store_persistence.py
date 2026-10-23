"""缺省落盘。

worker 的四个存储曾经缺省都是 `:memory:`：照文档起 worker，进程一重启
证据全丢，而且不报错。缺省值属于产品行为，所以它必须指向一个真的会留下来
的地方；"我这次就想用临时库"必须是一个显式选择。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from traceops.cli.worker import _resolve_store_paths
from traceops.runtime.paths import default_db_path, default_state_dir


class _Args:
    def __init__(self, **kwargs):
        self.ephemeral = kwargs.get("ephemeral", False)
        self.evidence_db = kwargs.get("evidence_db")
        self.ledger_db = kwargs.get("ledger_db")
        self.memory_db = kwargs.get("memory_db")


class TestTheStateDirectory:
    def test_it_follows_the_environment_variable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TRACEOPS_STATE_DIR", str(tmp_path / "state"))
        assert default_state_dir() == tmp_path / "state"

    def test_it_is_not_the_temp_directory(self, monkeypatch):
        monkeypatch.delenv("TRACEOPS_STATE_DIR", raising=False)
        import tempfile

        state = str(default_state_dir())
        assert not state.startswith(tempfile.gettempdir()), (
            "临时目录会被系统清理；证据 ID 指向一个消失的文件是最难排查的一类问题"
        )

    def test_db_paths_create_their_parent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TRACEOPS_STATE_DIR", str(tmp_path / "nested" / "state"))
        path = default_db_path("evidence.db")
        assert path.exists() is False
        assert path.parent.is_dir()


class TestStorePathResolution:
    def test_defaults_are_persistent_files_under_the_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TRACEOPS_STATE_DIR", str(tmp_path))
        paths = _resolve_store_paths(_Args())

        assert paths["evidence"] == tmp_path / "evidence.db"
        assert paths["ledger"] == tmp_path / "ledger.db"
        assert paths["memory"] == tmp_path / "memory.db"

    def test_ephemeral_is_the_only_way_to_get_memory(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TRACEOPS_STATE_DIR", str(tmp_path))
        assert _resolve_store_paths(_Args(ephemeral=True)) == {
            "evidence": None, "ledger": None, "memory": None,
        }

    def test_an_explicit_path_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TRACEOPS_STATE_DIR", str(tmp_path / "default"))
        explicit = tmp_path / "elsewhere" / "e.db"
        paths = _resolve_store_paths(_Args(evidence_db=str(explicit)))
        assert paths["evidence"] == explicit
        assert paths["ledger"] == tmp_path / "default" / "ledger.db"


class TestTheSmokeCommandStaysRepeatable:
    def test_two_self_checks_both_run(self, tmp_path):
        """run 账本缺省落盘后，固定 id 会让第二次自检被判成"重复消息"跳过。"""
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
               "TRACEOPS_STATE_DIR": str(tmp_path)}
        outputs = []
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, "-m", "traceops.cli.worker",
                 "--mode", "memory", "--once", "--self-check"],
                capture_output=True, text=True, timeout=60, env=env,
            )
            assert result.returncode == 0, result.stderr
            outputs.append(result.stderr)

        assert "processed=1" in outputs[0]
        assert "skipped=1" not in outputs[1].split("poll:")[-1], outputs[1]
        assert "processed=1" in outputs[1]
        assert (tmp_path / "ledger.db").exists()
        assert (tmp_path / "evidence.db").exists()
