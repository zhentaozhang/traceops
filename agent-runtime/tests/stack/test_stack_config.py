"""观测栈配置一致性（作为测试跑，而不是靠人记得执行脚本）。

为什么值得进测试套件：Grafana 数据源 uid 与代码默认值不一致时，
失败是运行时的 500（查不到数据源），不是启动报错。这种"配置漂移"
只有静态检查才能提前发现。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

AGENT_RUNTIME = Path(__file__).resolve().parents[2]


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """子进程需要显式带上 agent-runtime 目录：scripts/ 不在安装的包里。"""
    env = {**os.environ, "PYTHONPATH": str(AGENT_RUNTIME)}
    return subprocess.run(args, capture_output=True, text=True, timeout=60,
                          cwd=str(cwd) if cwd else None, env=env)


def test_stack_config_verifier_passes():
    result = _run([sys.executable, "-m", "scripts.verify_stack_config"])
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "RESULT: PASS" in result.stdout


def test_verifier_reports_checks_not_just_exit_code():
    """退出码 0 太弱：必须确认它真的检查了几项，而不是空跑。"""
    result = _run([sys.executable, "-m", "scripts.verify_stack_config"])
    assert result.stdout.count("PASS") >= 8, result.stdout


def test_compose_file_is_valid():
    """compose 语法本身必须能被 docker compose config 接受。

    Docker daemon 不可用时 `docker compose config` 仍可离线校验语法
    （它不需要连接 daemon），所以这条在无 Docker 环境也能跑。
    """
    stack = AGENT_RUNTIME.parent / "demo-environment" / "observability"
    result = _run(["docker", "compose", "config", "--quiet"], cwd=stack)
    if result.returncode != 0 and "Cannot connect" in result.stderr:
        pytest.skip("docker CLI unavailable")
    assert result.returncode == 0, result.stderr
