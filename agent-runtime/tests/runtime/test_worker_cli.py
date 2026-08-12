"""Worker CLI 测试：装配正确性与参数解析。

为什么测 CLI 而不是只测组件：装配错误（把 sink 接错、拓扑解析错、
数据源未注入）在单元测试里看不出来，只有真跑一次才暴露。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from traceops.cli.worker import build_adapters, build_topology, main


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "traceops.cli.worker", *args],
        capture_output=True, text=True, timeout=60,
    )


class TestSelfCheckMode:
    def test_self_check_processes_one_event(self):
        """核心：自检必须真的跑完一次调查，而不是空跑返回 0。"""
        result = _run_cli(["--mode", "memory", "--once", "--self-check"])

        assert result.returncode == 0, result.stderr
        assert "investigation.completed" in result.stdout
        assert "processed=1" in result.stderr
        assert "succeeded=1" in result.stderr

    def test_self_check_without_datasources_abstains(self):
        """没有数据源时必须如实弃权，而不是编造结论。"""
        result = _run_cli(["--mode", "memory", "--once", "--self-check"])
        assert '"INCONCLUSIVE"' in result.stdout
        assert '"abstained": true' in result.stdout

    def test_plain_once_without_self_check_does_nothing(self):
        result = _run_cli(["--mode", "memory", "--once"])
        assert result.returncode == 0


class TestTopologyParsing:
    def test_parses_caller_colon_callee(self):
        topology = build_topology(_args("api-gateway:order-service;order-service:redis"))
        assert topology is not None
        assert topology.dependencies("order-service") == {"redis"}
        assert topology.dependents("redis") == {"order-service"}

    def test_empty_topology_is_none(self):
        assert build_topology(_args("")) is None

    def test_invalid_edge_raises(self):
        with pytest.raises(SystemExit):
            build_topology(_args("no-colon-here"))

    def test_cycle_is_rejected(self):
        with pytest.raises(Exception):
            build_topology(_args("a:b;b:a"))


class TestAdapterWiring:
    def test_no_urls_means_no_adapters(self):
        assert build_adapters(_args("")) is None

    def test_partial_configuration_allowed(self):
        """只配一部分数据源是合法的：缺的那个走 missing_data 降级路径。"""
        adapters = build_adapters(_args("", prometheus="http://prom:9090"))
        assert adapters is not None
        assert adapters.prometheus is not None
        assert adapters.loki is None
        assert adapters.tempo is None

    def test_all_three_wired(self):
        adapters = build_adapters(_args(
            "", prometheus="http://p", loki="http://l", tempo="http://t"))
        assert adapters.prometheus and adapters.loki and adapters.tempo


class _Args:
    def __init__(self, topology: str = "", prometheus: str = "", loki: str = "", tempo: str = "") -> None:
        self.topology = topology
        self.prometheus_url = prometheus or None
        self.loki_url = loki or None
        self.tempo_url = tempo or None


def _args(topology: str = "", *, prometheus: str = "", loki: str = "", tempo: str = "") -> _Args:
    return _Args(topology, prometheus, loki, tempo)
