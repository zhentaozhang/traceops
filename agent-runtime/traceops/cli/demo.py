"""一次真正跑起来的调查，以及一份人能读的诊断 —— 不需要 Redis、不需要 Jaeger、不需要模型。

## 这个命令是给谁用的

给**第一次打开这个仓库的人**。README 里其它入口都要先起一堆东西（Redis Streams、
控制面、数据源、worker），于是"这东西到底产出什么"这件事只能靠读代码猜。

这里用一段**合成数据**（三个服务的指标 / 日志 / 追踪 / 一条变更事件）跑完整编排：
计划 → 采集 → 假设 → 关联 → 排序 → 报告。除了数据源是假造的，其它每一步都是真的。

## 诚实边界

- 数据是合成的，所以结论不能当作"系统能诊断真故障"的证据；
- 它证明的是另外一件事：**从一份杂乱的遥测到一个可引用的结论，这条链路是通的**，
  而且结论旁边就写着它引用了哪几条证据、还缺什么。

用法：

    PYTHONPATH=agent-runtime .venv/bin/python -m traceops.cli.demo
    PYTHONPATH=agent-runtime .venv/bin/python -m traceops.cli.demo --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import InvestigationRequest, RunContext, run_investigation
from traceops.rca.render import render_hypotheses, render_report
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

FAULT_START = datetime(2026, 3, 12, 14, 32, 0, tzinfo=UTC)
WINDOW_START = FAULT_START - timedelta(minutes=10)
WINDOW_END = FAULT_START + timedelta(minutes=5)
STEP_SECONDS = 60

#: 三个服务里谁说了什么。数值是编的，但**形状**与真实 Prometheus / Loki / Tempo 一致。
_SERIES: dict[str, list[float]] = {
    "checkout-service": [0.001, 0.001, 0.002, 0.004, 0.06, 0.42, 0.40],
    "payment-service": [0.002, 0.002, 0.003, 0.003, 0.01, 0.02, 0.02],
}

_LOGS: dict[str, list[str]] = {
    "payment-service": [
        "ERROR redis: could not get a resource from the pool (waited 3000ms)",
        "ERROR redis: connection pool exhausted (active=20 idle=0 max=20)",
        "WARN payment: charge request timed out after 3000ms",
    ],
}

_SPANS: dict[str, list[tuple[str, int]]] = {
    "payment-service": [
        ("POST /charge", 940),
        ("redis.command GET", 3100),
        ("redis.command SETEX", 2880),
    ],
}


def _empty_metric() -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "matrix", "result": []}}


def _empty_logs() -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "streams", "result": []}}


def _empty_traces() -> dict[str, Any]:
    return {"traces_considered": 0, "batches": []}


class DemoAdapters:
    """实现 `register_builtin_tools` 需要的只读方法，数据由本模块合成。"""

    #: 声明能力：没有 profile 能力，所以计划里不会出现火焰图查询
    #: （否则每次调用失败都会写进 missing_data，报告说"缺数据"而真相是"没配"）。
    capabilities = frozenset({"metric_series", "error_rate", "logs", "traces"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def query_metric_series(self, *, service: str, metric: str, start: str, end: str) -> dict[str, Any]:
        self.calls.append(("query_metric_series", service))
        return self._metric(service)

    def query_service_error_rate(self, *, service: str, start: str, end: str) -> dict[str, Any]:
        self.calls.append(("query_service_error_rate", service))
        return self._metric(service)

    def query_service_logs(
        self, *, service: str, start: str, end: str, filter: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("query_service_logs", service))
        messages = _LOGS.get(service)
        if not messages:
            return _empty_logs()
        base_ns = int(FAULT_START.timestamp() * 1_000_000_000)
        return {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"service": service, "level": "error"},
                        "values": [
                            [str(base_ns + i * 1_000_000_000), message]
                            for i, message in enumerate(messages)
                        ],
                    }
                ],
            },
        }

    def query_service_traces(
        self, *, service: str, start: str, end: str, min_duration_ms: int | None = None
    ) -> dict[str, Any]:
        self.calls.append(("query_service_traces", service))
        spans = _SPANS.get(service)
        if not spans:
            return _empty_traces()
        start_us = int(FAULT_START.timestamp() * 1_000_000)
        return {
            "traces_considered": 1,
            "batches": [
                {
                    "resource": {"service.name": service},
                    "spans": [
                        {
                            "traceID": "demo0000000000000000000000000001",
                            "spanID": f"{i:016x}",
                            "operationName": name,
                            "startTime": start_us,
                            "duration": duration_ms * 1000,
                            "tags": [{"key": "error", "value": False}],
                        }
                        for i, (name, duration_ms) in enumerate(spans)
                    ],
                }
            ],
        }

    def _metric(self, service: str) -> dict[str, Any]:
        series = _SERIES.get(service)
        if not series:
            return _empty_metric()
        last = FAULT_START.timestamp()
        values = [
            [last - STEP_SECONDS * (len(series) - 1 - i), float(v)]
            for i, v in enumerate(series)
        ]
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{"metric": {"service": service}, "values": values}],
            },
        }


def build_request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id="run-demo-1",
        incident_id="inc-demo-1",
        environment="demo-synthetic",
        service_hint="checkout-service",
        fault_start=FAULT_START,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        priority="high",
        alert_summary="checkout-service is slow and returning 5xx (p99 3.1s)",
        # 一条真实的变更：故障前三分钟把连接池从 50 调到 20。
        # 计划会把它当成一个 EVENT 证据源去取证（`_change_items`），而不是靠人记得。
        changes=[
            {
                "changed_at": (FAULT_START - timedelta(minutes=3)).isoformat(),
                "change_kind": "deployment",
                "service": "payment-service",
                "revision": "v2.31.0",
                "summary": "payment-service v2.31.0: redis pool size 50 -> 20",
            }
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run one investigation on synthetic data")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="also write the raw report JSON to this path")
    parser.add_argument("--verbose", action="store_true", help="print the raw summary too")
    args = parser.parse_args(argv)

    adapters = DemoAdapters()
    topology = ServiceTopology.from_edges(
        [
            ("checkout-service", "payment-service"),
            ("checkout-service", "redis"),
            ("payment-service", "redis"),
        ]
    )
    ctx = RunContext(
        store=EvidenceStore(),
        adapters=adapters,
        budget=BudgetManager(max_tool_calls=40),
        topology=topology,
        scope_depth=1,
    )

    print("== 一次调查（数据源为合成数据，编排与报告是真的）==")
    result = run_investigation(build_request(), ctx)
    report = result.report
    if report is None:
        print(f"本次调查没有产出报告：status={result.status} errors={result.errors}")
        return 1

    print()
    print(render_report(report, cost={"tool_calls": result.tool_calls,
                                      "tokens_used": result.tokens_used}))
    print()
    print(render_hypotheses(report))
    if args.verbose:
        print()
        print("原始 summary：")
        print("  " + report.summary)

    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"\n完整报告已写入 {path}（{len(report.model_dump_json())} 字节）",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
