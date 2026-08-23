"""真端到端冒烟作为测试（真实 socket，不是假 adapter）。

为什么值得进测试套件：`smoke_loop` 用假 adapter 直接返回 dict，
它证明了编排正确，但证明不了适配器在真实网络上能工作。
两者缺一不可 —— 而"适配器 URL 拼错"这类问题只在真实 HTTP 下暴露。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

AGENT_RUNTIME = Path(__file__).resolve().parents[2]


def test_e2e_over_real_http_passes():
    env = {**os.environ, "PYTHONPATH": str(AGENT_RUNTIME)}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.e2e_with_fake_stack"],
        capture_output=True, text=True, timeout=180, env=env)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "RESULT: PASS" in result.stdout


def test_e2e_reports_enough_checks():
    """退出码 0 不足以说明覆盖：确认它真跑了几项检查。"""
    env = {**os.environ, "PYTHONPATH": str(AGENT_RUNTIME)}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.e2e_with_fake_stack"],
        capture_output=True, text=True, timeout=180, env=env)
    assert result.stdout.count("PASS") >= 8, result.stdout


def test_e2e_finds_the_downstream_root_cause():
    """端到端结论必须正确 —— 否则"链路通了"没有意义。"""
    env = {**os.environ, "PYTHONPATH": str(AGENT_RUNTIME)}
    result = subprocess.run(
        [sys.executable, "-m", "scripts.e2e_with_fake_stack"],
        capture_output=True, text=True, timeout=180, env=env)
    assert "root cause is the downstream dependency" in result.stdout
    assert "diagnosis SUPPORTED" in result.stdout
