"""影响层证据不能生成候选，但**必须**参与机制判定。

## 问题（由 F-006 场景暴露）

现在 `_hypothesize_node` 只把 investigation 层证据喂给 `generate_hypotheses`：

```python
investigation_evidence = [e for e in evidence_list
                          if e.query_params.get("_tier") == INVESTIGATION_TIER]
hypotheses = generate_hypotheses(investigation_evidence, ...)
```

这条规则的**目的**是对的：影响层是"上游调用方 / 兄弟"，它们的异常是症状，
不该成为候选根因。但规则实现过头了——它连带丢掉了影响层证据对
**已有候选的机制描述**。

F-006 的实测后果：

```
影响层 inventory-service 的日志："ERROR ... redis timeout ... could not reserve stock"
  → attribute_evidence 正确归因给 redis（这份证据是"关于 redis"的）
  → 它能支撑 redis/pool_exhaustion（"池耗尽"机制）
  → 但因为它在 impact tier，被整条丢掉

结果：redis 只剩自己的弱信号（错误率 0.07 + 慢 span）
  → 机制判成 availability_degradation（GT 是 pool_exhaustion）→ FT@1 = 0
```

## 正确规则（三件事分开）

| 证据的角色 | 能不能做 | 理由 |
|---|---|---|
| 生成**候选服务** | ❌ 影响层不行 | 否则把调用方/兄弟当根因 |
| 描述**已有候选的机制** | ✅ 影响层可以 | 归因后它确实是"关于根因服务"的证据 |
| 进传播链 / 影响范围 | ✅ 必须 | 本来就是它的用途 |

判据：影响层证据只有在 `attribute_evidence` 把它归因到**一个已存在的候选服务**
时才可用于机制判定。归因不到本候选的证据依然被排除。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import Evidence, EvidenceType
from traceops.orchestration.detectors import attribute_evidence, generate_hypotheses
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)

TOPOLOGY = ServiceTopology.from_edges([
    ("api-gateway", "order-service"),
    ("order-service", "redis"),
    ("inventory-service", "redis"),
])


def _log(service: str, message: str, tier: str = "investigation") -> Evidence:
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.LOG,
        source_id="loki", service_id=service, observed_at=TS,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id="qt-error-logs-v1", query_params={"_tier": tier},
        normalized_data={"kind": "log", "total_records": 1, "records": [
            {"timestamp": TS.isoformat(), "message": message, "labels": {"level": "error"}}]},
        checksum="",
    )


def _metric(service: str, value: str, tier: str = "investigation") -> Evidence:
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.METRIC,
        source_id="prom", service_id=service, observed_at=TS,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id="qt-error-rate-v1", query_params={"_tier": tier},
        normalized_data={"kind": "metric", "result_type": "matrix", "result_count": 1,
                         "total_records": 1,
                         "series": [{"metric": {}, "values": [[TS.timestamp(), value]]}]},
        checksum="",
    )


class TestAttributionBridgesTheTiers:
    def test_sibling_log_is_attributed_to_the_root_service(self):
        """兄弟服务的日志讲 redis，就是"关于 redis"的证据（归因先于分层）。"""
        evidence = _log("inventory-service",
                        "ERROR inventory-service: redis timeout, could not reserve stock",
                        tier="impact")
        attribution = attribute_evidence(evidence, TOPOLOGY)
        assert attribution is not None and attribution[0] == "redis"

    def test_impact_evidence_can_describe_mechanism_of_existing_candidate(self):
        """影响层证据归因到已有候选 → 应当成为该候选的机制证据。"""
        investigation = [
            _metric("redis", "0.07"),          # 弱信号，只够生成 availability
            _log("redis", "ERROR redis timeout"),  # 让 redis 成为候选
        ]
        sibling_log = _log(
            "inventory-service",
            "ERROR inventory-service: redis timeout - connection pool exhausted",
            tier="impact")

        hypotheses = generate_hypotheses(
            investigation + [sibling_log],
            investigation_run_id="r", topology=TOPOLOGY,
            mechanism_only_services={"inventory-service"},
        )
        fault_types = {h.fault_type for h in hypotheses if h.candidate_service == "redis"}
        assert "pool_exhaustion" in fault_types, (
            "sibling evidence about redis must be able to describe its mechanism; "
            f"got {sorted(fault_types)}")

    def test_impact_evidence_does_not_create_a_candidate_for_the_sibling(self):
        """但影响层证据**不能**让兄弟自己变成候选根因。"""
        sibling_log = _log("inventory-service",
                           "ERROR inventory-service: something broke locally",
                           tier="impact")
        hypotheses = generate_hypotheses([sibling_log], investigation_run_id="r",
                                          topology=TOPOLOGY,
                                          mechanism_only_services={"inventory-service"})
        assert hypotheses == [], (
            "impact-tier evidence alone must not propose a root cause")

    def test_impact_evidence_about_unrelated_service_is_ignored(self):
        """归因不到任何候选的影响层证据，不得凭空造出机制。"""
        unrelated = _log("payments", "ERROR payments: card declined", tier="impact")
        hypotheses = generate_hypotheses([unrelated], investigation_run_id="r",
                                          topology=TOPOLOGY,
                                          mechanism_only_services={"payments"})
        assert hypotheses == []
