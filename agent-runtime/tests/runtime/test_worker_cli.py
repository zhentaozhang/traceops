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


class TestMCPAdapterWiring:
    """MCP 是「生产推荐」的数据源接入方式，必须能从 CLI 启用。

    背景：`MCPDatasourceAdapters` 早就写好了，但**没有任何生产调用点**——
    worker CLI 只有 --prometheus-url / --loki-url / --tempo-url 三个直连开关，
    `MCPDatasourceAdapters` 只被 `scripts/verify_stack_config.py` 反射检查过参数名。
    也就是说这条「推荐路径」其实是悬空的：配了也没法启用。
    """

    def test_mcp_url_builds_mcp_adapters(self):
        adapters = build_adapters(_args("", mcp="http://localhost:8000/mcp"))
        assert adapters is not None
        # 必须是 MCP 实现，而不是直连实现
        assert type(adapters).__name__ == "MCPDatasourceAdapters"

    def test_mcp_url_does_not_require_direct_urls(self):
        """MCP 单独配置即可工作，不该强制同时填三个直连地址。

        断言的是**行为**（三个方法都被 MCP 实现覆盖），
        而不是内部属性 `adapters.prometheus`——MCP 实现根本不用这些属性，
        拿属性判断装配会得出错误结论。
        """
        adapters = build_adapters(_args("", mcp="http://localhost:8000/mcp"))
        mcp_class = type(adapters)
        for method in ("query_metric_series", "query_service_error_rate",
                       "query_service_logs", "query_service_traces"):
            # 每个方法都必须由 MCP 实现自己定义，而不是继承自基类走直连分支
            assert method in vars(mcp_class), f"{method} not overridden by MCP adapters"

    def test_direct_urls_still_work_when_mcp_absent(self):
        """不配 MCP 时行为不变：直连适配器照常装配。"""
        adapters = build_adapters(_args("", prometheus="http://p"))
        assert type(adapters).__name__ == "DatasourceAdapters"

    def test_mcp_takes_precedence_over_direct_urls(self):
        """两者都配时用 MCP（生产推荐路径），避免两套数据源并存导致证据来源混淆。"""
        adapters = build_adapters(_args(
            "", prometheus="http://p", loki="http://l", tempo="http://t",
            mcp="http://localhost:8000/mcp"))
        assert type(adapters).__name__ == "MCPDatasourceAdapters"


class _Args:
    def __init__(self, topology: str = "", prometheus: str = "", loki: str = "", tempo: str = "",
                 mcp: str = "") -> None:
        self.topology = topology
        self.prometheus_url = prometheus or None
        self.loki_url = loki or None
        self.tempo_url = tempo or None
        self.mcp_url = mcp or None


def _args(topology: str = "", *, prometheus: str = "", loki: str = "", tempo: str = "",
          mcp: str = "") -> _Args:
    return _Args(topology, prometheus, loki, tempo, mcp)


class TestRedisModeFailure:
    """连不上 Redis 时必须"可读地失败"，而不是栈炸或静默成功。

    为什么重要：容器编排里退出码决定是否重启/告警。
    返回 0 会让一个根本没连上 Redis 的 worker 看起来"正常运行"，
    这是最危险的假阳性。
    """

    def test_unreachable_redis_exits_nonzero_with_readable_message(self):
        result = _run_redis_cli(["--mode", "redis", "--port", "6399", "--once"])

        assert result.returncode == 3, result.stderr
        assert "cannot connect to Redis" in result.stderr
        # 不应该把整个栈泼给用户
        assert "Traceback" not in result.stderr

    def test_message_names_the_target(self):
        result = _run_redis_cli(["--mode", "redis", "--port", "6399", "--once"])
        assert "localhost:6399" in result.stderr


def _run_redis_cli(args: list[str]) -> subprocess.CompletedProcess:
    import os
    from pathlib import Path

    env = {**os.environ}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return subprocess.run([sys.executable, "-m", "traceops.cli.worker", *args],
                          capture_output=True, text=True, timeout=60, env=env)


class TestPurgeCommand:
    """--purge 的安全行为：必须拒绝在没有明确数据库/SOP 的情况下"清理"。"""

    def test_purge_without_evidence_db_refuses(self):
        """不指定数据库就清理是不可接受的 —— 可能删错东西。"""
        result = _run([sys.executable, "-m", "traceops.cli.worker", "--purge"])
        assert result.returncode == 2
        assert "requires --evidence-db" in result.stderr

    def test_purge_dry_run_deletes_nothing(self, tmp_path):
        import os
        from pathlib import Path

        db = tmp_path / "e.db"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        result = subprocess.run(
            [sys.executable, "-m", "traceops.cli.worker", "--purge",
             "--evidence-db", str(db), "--purge-dry-run"],
            capture_output=True, text=True, timeout=60, env=env)
        assert result.returncode == 0
        assert "dry_run=True" in result.stderr

    def test_empty_closed_list_warns_instead_of_pretending(self, tmp_path):
        """没有"已关闭事故"时，保护规则会保留一切 —— 必须明说，不能让人误以为清理过。"""
        import os
        from pathlib import Path

        db = tmp_path / "e2.db"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        result = subprocess.run(
            [sys.executable, "-m", "traceops.cli.worker", "--purge", "--evidence-db", str(db)],
            capture_output=True, text=True, timeout=60, env=env)
        assert "no closed incidents supplied" in result.stderr


def _run(args: list[str]) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ}
    env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).resolve().parents[2])
    return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)
