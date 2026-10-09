"""端到端：worker 的失败事件 → Redis → 控制面 → 重试排期。

这不是模拟：用的是 worker **自己的** `InvestigationWorker`、**自己的**
`failed_event()`、**自己的** `RedisStreamEventSink`，以及真实 Redis。

做法：把一条 `investigation.requested` XADD 进流里，让 worker 消费它；
把 `run_investigation` 换成会抛异常的桩，于是走的是 worker 里真实存在的
那条 `except Exception` 分支（INTERNAL_ERROR → retryable=True）。

为什么不直接让真实调查失败：那需要真实数据源。而没有适配器时调查会
"优雅降级"成 INCONCLUSIVE 而不是 FAILED —— 降级是设计好的行为，
不该为了造一个失败事件而破坏它。

用法：
    PYTHONPATH=agent-runtime .venv/bin/python -m scripts.e2e_retry_loop \
        --incident <incident_id> --run <run_id> --host 127.0.0.1 --port 6379
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from traceops.cli.worker import derive_request_from_event
from traceops.orchestration.graph import RunContext
from traceops.runtime import worker as worker_module
from traceops.runtime.redis_sink import RedisStreamEventSink
from traceops.runtime.redis_source import RedisStreamEventSource
from traceops.runtime.redis_transport import RespRedisTransport
from traceops.runtime.worker import InvestigationWorker


def _request_document(incident: str, run: str, service: str) -> dict[str, Any]:
    return {
        "envelope": {
            "event_id": "evt-e2e-0000-0000-0000-000000000001",
            "event_type": "investigation.requested",
            "schema_version": "1.0",
            "occurred_at": "2026-10-31T16:00:00+00:00",
            "correlation_id": incident,
            "causation_id": None,
            "aggregate_id": run,
            "aggregate_version": 1,
            "producer": "control-plane",
        },
        "payload": {
            "investigation_run_id": run,
            "incident_id": incident,
            "service_hint": service,
            "environment": "demo",
            "time_range": {
                "start": "2026-10-31T15:55:00+00:00",
                "end": "2026-10-31T16:00:00+00:00",
            },
            "config_version": "cfg-2026.10",
            "priority": "normal",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incident", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--service", default="order-service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--stream", default="traceops.events")
    parser.add_argument("--group", default="traceops-workers")
    parser.add_argument("--consumer", default="worker-e2e")
    args = parser.parse_args()

    transport = RespRedisTransport(host=args.host, port=args.port)
    transport.connect()

    # 把请求塞进真实的流里，worker 从流里消费它 —— 走的是真实路径。
    document = _request_document(args.incident, args.run, args.service)
    entry_id = transport.xadd(
        args.stream, json.dumps(document, ensure_ascii=False, sort_keys=True))
    print(f"published investigation.requested: entry_id={entry_id}")

    source = RedisStreamEventSource(
        transport, stream=args.stream, group=args.group, consumer=args.consumer)
    sink = RedisStreamEventSink(transport, stream=args.stream)

    # 让调查抛异常：走的是 worker 里真实存在的那条 except 分支。
    def _boom(*_a: Any, **_kw: Any):
        raise RuntimeError("simulated datasource outage during investigation")

    worker_module.run_investigation = _boom  # type: ignore[assignment]

    worker = InvestigationWorker(
        source=source,
        sink=sink,
        derive_request=derive_request_from_event,
        build_context=lambda request: RunContext(store=None, adapters=None),
    )
    stats = worker.poll_once()
    print(f"stats: processed={stats.processed} failed={stats.failed} "
          f"skipped={stats.skipped_duplicate} publish_failed={stats.publish_failed}")
    transport.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
