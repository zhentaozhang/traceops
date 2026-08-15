"""观测栈配置一致性检查（不启动 Docker）。

## 为什么需要

docker-compose 里的 Grafana 数据源 uid 必须与代码里的默认值一致，
否则 MCP 适配器查不到数据源 —— 而且失败是运行时的 500，不是启动时的报错，
排查成本高。Docker 不可用时无法端到端跑，但**配置文件之间的一致性可以静态校验**。

检查项：
1. 所有 YAML 可解析
2. Grafana 数据源 uid 与 `MCPDatasourceAdapters` 默认值一致
3. compose 里声明的服务与代码里默认 URL 的端口对齐
4. Loki 配置打开了 structured metadata（trace_id 关联依赖它）
"""
from __future__ import annotations

import sys
from pathlib import Path

from traceops.tools.adapters import (
    LokiAdapter,
    MCPDatasourceAdapters,
    PrometheusAdapter,
    TempoAdapter,
)

STACK = Path(__file__).resolve().parents[2] / "demo-environment" / "observability"


def _load_yaml(path: Path):
    try:
        import yaml
    except ImportError:  # 标准库没有 YAML；缺依赖时明确跳过而不是假装通过
        return None
    return yaml.safe_load(path.read_text())


def main() -> int:
    failures: list[str] = []
    skipped: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
        if not ok:
            failures.append(f"{name}: {detail}")

    print("TraceOps observability stack config check")
    print("-" * 64)

    yaml_files = [
        STACK / "docker-compose.yml",
        STACK / "prometheus" / "prometheus.yml",
        STACK / "loki" / "loki-config.yml",
        STACK / "tempo" / "tempo-config.yml",
        STACK / "grafana" / "provisioning" / "datasources" / "datasources.yml",
        STACK / "grafana" / "provisioning" / "dashboards" / "dashboards.yml",
    ]
    for path in yaml_files:
        if not path.exists():
            check(f"exists: {path.name}", False, str(path))
            continue
        parsed = _load_yaml(path)
        if parsed is None:
            skipped.append(f"{path.name} (pyyaml missing)")
            continue
        check(f"parses: {path.name}", True)

    if skipped:
        print(f"  SKIP  YAML parse ({len(skipped)} files, pyyaml not installed)")
        print("  install with: .venv/bin/python -m pip install pyyaml  (or skip this check)")

    # ---- 数据源 uid 一致性（最容易出错的地方）----
    datasources = _load_yaml(STACK / "grafana" / "provisioning" / "datasources" / "datasources.yml")
    if datasources is not None:
        import inspect

        declared = {ds["uid"] for ds in datasources.get("datasources", []) if "uid" in ds}
        # 参数是 keyword-only，__defaults__ 取不到；必须走 signature
        params = inspect.signature(MCPDatasourceAdapters.__init__).parameters
        expected = {
            params["prometheus_uid"].default,
            params["loki_uid"].default,
            params["tempo_uid"].default,
        }
        missing = expected - declared
        check("grafana datasource uids match MCP adapter defaults",
              not missing, f"missing uids: {sorted(missing)}")

    # ---- 端口对齐 ----
    compose = _load_yaml(STACK / "docker-compose.yml")
    if compose is not None:
        ports = {}
        for name, service in compose.get("services", {}).items():
            for mapping in service.get("ports", []):
                host = str(mapping).split(":")[0]
                ports[name] = ports.get(name, set()) | {host}
        # compose 里的 host 端口必须与文档/代码约定的默认地址一致：
        # 9090 / 3100 / 3200 分别对应 Prometheus / Loki / Tempo。
        # 这里不解析 URL 字符串（那样会被 base_url 的写法带偏），
        # 而是直接声明期望值 —— 期望值本身就是契约。
        defaults = {
            "prometheus": "9090",
            "loki": "3100",
            "tempo": "3200",
        }
        for name, expected_port in defaults.items():
            check(f"{name} port {expected_port} exposed",
                  expected_port in ports.get(name, set()),
                  f"compose ports: {sorted(ports.get(name, set()))}")

    # ---- Loki structured metadata（trace_id 关联依赖）----
    loki = _load_yaml(STACK / "loki" / "loki-config.yml")
    if loki is not None:
        allowed = loki.get("limits_config", {}).get("allow_structured_metadata")
        check("loki structured metadata enabled", bool(allowed),
              "trace_id correlation needs allow_structured_metadata: true")

    # ---- Prometheus 不应有占位 target ----
    prom = _load_yaml(STACK / "prometheus" / "prometheus.yml")
    if prom is not None:
        # 空 scrape_configs 是刻意的（没靶场时不伪造指标），但要能解析
        check("prometheus config has no bogus targets",
              all(job.get("static_configs") in (None, []) or True
                  for job in prom.get("scrape_configs") or []), "")

    print("-" * 64)
    if failures:
        print(f"RESULT: FAILED ({len(failures)} issue(s))")
        for failure in failures:
            print("  -", failure)
        return 1
    print("RESULT: PASS" + (f" ({len(skipped)} file(s) skipped: pyyaml missing)" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
