"""证据必须能追溯到"实际执行的查询语句"。

## 缺口

`query_template_id` 告诉你"用了哪个模板"，`query_params` 告诉你"填了什么参数"。
但审计真正需要的是**渲染后的查询语句**——因为同一模板 + 同一参数，在不同
模板版本下可能生成不同的查询。

蓝图 §6.1 明确要求证据可追溯到查询条件；只存模板 ID 是追溯不到的。

## 为什么单独一个字段而不是塞进 query_params

`query_params` 是"输入"，`rendered_query` 是"输出"。混在一起会导致：
- 参数 schema 校验会把 rendered_query 当成非法额外参数（之前踩过）
- 审计时无法区分"用户传的"和"系统生成的"
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import Evidence, EvidenceType

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW = (TS - timedelta(minutes=10), TS + timedelta(minutes=10))


def make_evidence(**overrides) -> Evidence:
    base = dict(
        incident_id="inc-001", investigation_run_id="run-001",
        evidence_type=EvidenceType.METRIC, source_id="prometheus",
        service_id="redis", observed_at=TS,
        window_start=WINDOW[0], window_end=WINDOW[1],
        query_template_id="qt-error-rate-v1",
        query_params={"service": "redis"},
        normalized_data={"kind": "metric", "series": []},
    )
    base.update(overrides)
    return Evidence(**base)


class TestRenderedQueryField:
    def test_rendered_query_is_stored(self):
        evidence = make_evidence(
            rendered_query='sum(rate(http_requests_total{service="redis"}[1m]))')
        assert evidence.rendered_query is not None
        assert 'service="redis"' in evidence.rendered_query

    def test_rendered_query_is_optional(self):
        """不是所有证据都有查询语句（如变更事件来自控制面事件，不是查询）。"""
        evidence = make_evidence()
        assert evidence.rendered_query is None

    def test_rendered_query_survives_serialisation(self):
        """必须能被持久化——不能只是运行时字段。"""
        evidence = make_evidence(rendered_query="up{job=\"redis\"}")
        dumped = evidence.model_dump(mode="json")
        assert dumped["rendered_query"] == 'up{job="redis"}'
        restored = Evidence.model_validate(dumped)
        assert restored.rendered_query == dumped["rendered_query"]

    def test_rendered_query_does_not_affect_checksum(self):
        """checksum 只对 normalized_data 计算 —— 查询语句变了但数据相同，
        不应导致"篡改"误报。"""
        from traceops.evidence.store import compute_checksum

        a = make_evidence(rendered_query="q1")
        b = make_evidence(rendered_query="q2")
        assert compute_checksum(a) == compute_checksum(b)


class TestAuditTrail:
    def test_evidence_carries_full_query_trail(self):
        """三条审计信息必须齐全：模板 ID、输入参数、实际语句。"""
        evidence = make_evidence(
            query_template_id="qt-error-logs-v1",
            query_params={"service": "redis", "filter": "timeout"},
            rendered_query='{service="redis",level="error"} |= "timeout"',
        )
        assert evidence.query_template_id == "qt-error-logs-v1"
        assert evidence.query_params["filter"] == "timeout"
        assert "timeout" in evidence.rendered_query
