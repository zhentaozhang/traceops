"""调查 Worker 的可运行入口（包内版本）。

把已经在测试里验证过的组件装配成一个真正能跑起来的进程：

    事件源（Redis Streams / 内存） → InvestigationWorker → 结果事件出口

用法：
    # 内存模式（自检，不连外部依赖）
    .venv/bin/python -m scripts.worker_cli --mode memory

    # Redis 模式（需要可用的 transport）
    .venv/bin/python -m scripts.worker_cli --mode redis --host localhost --port 6379

设计说明：
- 参数与环境变量都能配（容器里用 env 更方便）
- 启动时打印配置摘要，不打印凭证
- 单轮模式的 `--once` 用于 CI / 手动验证，避免无限循环
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext
from traceops.runtime.budget import BudgetManager
from traceops.runtime.deadletter import InMemoryDeadLetterSink
from traceops.runtime.retry import RetryPolicy
from traceops.evidence.retention import RetentionPolicy
from traceops.runtime.run_ledger import RunLedger
from traceops.runtime.worker import (
    InMemoryEventSink,
    InMemoryEventSource,
    InvestigationWorker,
    derive_request_from_event,
    dumps,
)
from traceops.tools.adapters import (
    DatasourceAdapters,
    LokiAdapter,
    PrometheusAdapter,
    TempoAdapter,
)
from traceops.tools.gateway import ToolGateway, register_builtin_tools
from traceops.topology.graph import TopologyError, ServiceTopology


def _self_check_event() -> dict:
    """与 control-plane EventMessages 产出一致的形状（含 Redis entry 摊平）。"""
    return {
        "id": "selfcheck-1",
        "payload": {
            "envelope": {
                "event_id": "evt-11111111-2222-3333-4444-555555555555",
                "event_type": "investigation.requested",
                "schema_version": "1.0",
                "occurred_at": "2026-10-30T10:06:00Z",
                "correlation_id": "inc-selfcheck",
                "causation_id": None,
                "aggregate_id": "run-selfcheck",
                "aggregate_version": 1,
                "producer": "control-plane",
            },
            "payload": {
                "investigation_run_id": "run-selfcheck",
                "incident_id": "inc-selfcheck",
                "service_hint": "order-service",
                "environment": "demo",
                "time_range": {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:06:00Z"},
                "config_version": "cfg-selfcheck",
                "priority": "normal",
                "scope_depth": 1,
            },
        },
    }


def build_adapters(args) -> DatasourceAdapters | None:
    """按配置构造数据源适配器。

    未配置的适配器留 None —— 对应的工具调用会抛错，编排层把它记入
    missing_data 并继续（这是设计好的降级路径，不是缺陷）。

    两条接入方式，**MCP 优先**：
    - `--mcp-url`：走 Grafana MCP Server（生产推荐，不直连各数据源）
    - `--prometheus-url` / `--loki-url` / `--tempo-url`：直连各数据源（调试用）

    两者都配时选 MCP：同一次调查的证据不能一半来自直连、一半来自 MCP，
    否则"这条证据是从哪个数据源来的"就说不清（破坏证据可追溯性）。
    """
    if getattr(args, "mcp_url", None):
        from traceops.tools.adapters import GrafanaMCPClient, MCPDatasourceAdapters

        return MCPDatasourceAdapters(GrafanaMCPClient(args.mcp_url))

    prom = PrometheusAdapter(args.prometheus_url) if args.prometheus_url else None
    loki = LokiAdapter(args.loki_url) if args.loki_url else None
    tempo = TempoAdapter(args.tempo_url) if args.tempo_url else None
    if prom is None and loki is None and tempo is None:
        return None
    return DatasourceAdapters(prometheus=prom, loki=loki, tempo=tempo)


def build_topology(args) -> ServiceTopology | None:
    """拓扑从 `caller:callee` 形式的分号列表解析，例如 `api-gateway:order-service`。"""
    if not args.topology:
        return None
    edges = []
    for item in args.topology.split(";"):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise SystemExit(f"invalid topology edge {item!r}: expected 'caller:callee'")
        caller, callee = item.split(":", 1)
        edges.append((caller.strip(), callee.strip()))
    return ServiceTopology.from_edges(edges)


def _describe_adapters(adapters, args) -> list[str]:
    """打印实际生效的数据源。

    为什么不直接读 `adapters.prometheus/loki/tempo`：那是**直连**实现的内部属性，
    `MCPDatasourceAdapters` 不设这些字段。曾经因此打出 `adapters=[]`，
    明明 MCP 已经采到了证据，日志却说"没有数据源"——排查时会把方向带偏。
    """
    if adapters is None:
        return []
    if type(adapters).__name__ == "MCPDatasourceAdapters":
        return [f"mcp:{getattr(args, 'mcp_url', '')}"]
    return [
        name
        for name, adapter in (
            ("prometheus", adapters.prometheus),
            ("loki", adapters.loki),
            ("tempo", adapters.tempo),
        )
        if adapter
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TraceOps investigation worker")
    parser.add_argument("--mode", choices=["memory", "redis"], default="memory")
    parser.add_argument("--host", default=os.environ.get("TRACEOPS_REDIS_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRACEOPS_REDIS_PORT", "6379")),
                        )
    parser.add_argument("--stream", default=os.environ.get("TRACEOPS_STREAM", "traceops.events"))
    parser.add_argument("--group", default=os.environ.get("TRACEOPS_GROUP", "traceops-workers"))
    parser.add_argument("--consumer", default=os.environ.get("TRACEOPS_CONSUMER", "worker-1"))
    parser.add_argument("--mcp-url", default=os.environ.get("TRACEOPS_MCP_URL"),
                        help="Grafana MCP Server URL (production path); overrides the three direct URLs")
    parser.add_argument("--prometheus-url", default=os.environ.get("TRACEOPS_PROMETHEUS_URL"))
    parser.add_argument("--loki-url", default=os.environ.get("TRACEOPS_LOKI_URL"))
    parser.add_argument("--tempo-url", default=os.environ.get("TRACEOPS_TEMPO_URL"))
    parser.add_argument("--topology", default=os.environ.get("TRACEOPS_TOPOLOGY", ""),
                        help="caller:callee pairs separated by ';'")
    parser.add_argument("--evidence-db", default=os.environ.get("TRACEOPS_EVIDENCE_DB"),
                        help="SQLite path; omit for in-memory evidence")
    parser.add_argument("--ledger-db", default=os.environ.get("TRACEOPS_LEDGER_DB"),
                        help="SQLite path for run dedup; omit for in-memory")
    parser.add_argument("--max-tool-calls", type=int, default=int(
        os.environ.get("TRACEOPS_MAX_TOOL_CALLS", "40")))
    parser.add_argument("--once", action="store_true", help="process a single batch then exit")
    parser.add_argument("--purge", action="store_true",
                        help="run evidence retention cleanup then exit (requires --evidence-db)")
    parser.add_argument("--purge-dry-run", action="store_true",
                        help="with --purge: report what would be deleted, delete nothing")
    parser.add_argument("--retain-days", type=int, default=int(
        os.environ.get("TRACEOPS_RETAIN_DAYS", "30")))
    parser.add_argument("--closed-incidents", default=os.environ.get("TRACEOPS_CLOSED_INCIDENTS", ""),
                        help="comma separated incident ids considered closed")
    parser.add_argument("--self-check", action="store_true",
                        help="memory mode: inject one synthetic event to verify wiring")
    args = parser.parse_args(argv)

    store = EvidenceStore(path=Path(args.evidence_db) if args.evidence_db else None)
    adapters = build_adapters(args)
    topology = build_topology(args)
    dead_letters = InMemoryDeadLetterSink()
    # 账本必须持久化：否则重启后未 ACK 的消息会被重复调查
    ledger = RunLedger(path=Path(args.ledger_db) if args.ledger_db else None)
    sink = InMemoryEventSink()

    if args.purge:
        if args.evidence_db is None:
            print("--purge requires --evidence-db (refusing to guess a database)", file=sys.stderr)
            return 2
        closed = {i.strip() for i in args.closed_incidents.split(",") if i.strip()}
        if not closed:
            # 没有"已关闭事故"清单时，require_closed_incident=True 会保护一切。
            # 明确告知，而不是让用户以为清理跑过了。
            print("warning: no closed incidents supplied; closed-incident protection "
                  "will retain everything", file=sys.stderr)
        from datetime import UTC as _UTC, datetime as _datetime

        result = store.purge(
            policy=RetentionPolicy(retain_days=args.retain_days),
            now=_datetime.now(_UTC),
            closed_incidents=closed,
            dry_run=args.purge_dry_run,
        )
        print(f"purge: purged={result.purged} protected={result.protected} "
              f"retained={result.retained} dry_run={result.dry_run}", file=sys.stderr)
        return 0

    if args.mode == "redis":
        from traceops.runtime.redis_transport import RespRedisTransport
        from traceops.runtime.redis_source import RedisStreamEventSource

        transport = RespRedisTransport(host=args.host, port=args.port)
        try:
            transport.connect()
        except Exception as exc:
            # 连不上是配置/环境问题，不是崩溃：给一行可读信息 + 非零退出码，
            # 让容器/CI 能判定失败，而不是被栈淹没后误以为启动了
            print(f"cannot connect to Redis at {args.host}:{args.port}: {exc}", file=sys.stderr)
            return 3
        source = RedisStreamEventSource(
            transport, stream=args.stream, group=args.group, consumer=args.consumer)
    else:
        source = InMemoryEventSource()
        if args.self_check:
            # 自检：注入一条形状与 Java 产出完全一致的事件，验证装配是否正确。
            # 这比"空跑一次返回 0"有用得多——空跑永远绿，什么也没证明。
            source.push(_self_check_event())

    def build_context(request: InvestigationRequest) -> RunContext:
        gateway = ToolGateway()
        register_builtin_tools(gateway, adapters)
        return RunContext(
            store=store,
            adapters=adapters,
            budget=BudgetManager(max_tool_calls=args.max_tool_calls),
            gateway=gateway,
            topology=topology,
        )

    worker = InvestigationWorker(
        source=source, sink=sink, derive_request=derive_request_from_event,
        build_context=build_context, dead_letters=dead_letters,
        retry_policy=RetryPolicy(max_attempts=3),
        ledger=ledger,
    )

    print(f"worker started: mode={args.mode} stream={args.stream} group={args.group} "
          f"consumer={args.consumer} adapters={_describe_adapters(adapters, args)}",
          file=sys.stderr)

    try:
        while True:
            stats = worker.poll_once()
            for message in sink.messages:
                print(dumps(message), flush=True)
            sink.messages.clear()
            for record in dead_letters.records:
                print(f"dead-letter: {dumps(record.to_dict())}", file=sys.stderr)
            dead_letters.records.clear()
            if stats.processed or stats.failed or stats.skipped_duplicate:
                print(f"poll: processed={stats.processed} succeeded={stats.succeeded} "
                      f"failed={stats.failed} skipped={stats.skipped_duplicate}",
                      file=sys.stderr)
            if args.once:
                return 0
    except TopologyError as exc:
        print(f"topology error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("worker stopped", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
