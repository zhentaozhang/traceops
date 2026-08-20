"""证据保留策略测试。

## 为什么必须有

证据会无限增长：每次调查 3~12 条，每个事故可能多轮。没有保留策略时：

1. 磁盘无限增长（SQLite 文件会持续变大，且 VACUUM 之前不会释放）
2. 事故关闭后证据已无审计价值，但仍占用查询开销

## 设计的谨慎之处

保留策略**不能**"到点就删"。原因：
- 事故还在处理中（未 CLOSED）时，证据必须保留，即使超过保留期
- 关系到证据的引用完整性：删了一条被报告引用的证据，报告就变成不可追溯的

所以删除必须有引用保护：任何被报告/关系引用的证据都不能删。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from traceops.domain.models import Evidence, EvidenceRelation, EvidenceRelationType, EvidenceType
from traceops.evidence.normalizer import normalize
from traceops.evidence.retention import RetentionPolicy, RetentionResult
from traceops.evidence.store import EvidenceStore

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW = (TS - timedelta(minutes=10), TS + timedelta(minutes=10))


def make_evidence(at: datetime, service: str = "redis") -> Evidence:
    return normalize(
        evidence_type=EvidenceType.METRIC, source_id="prometheus",
        incident_id="inc-001", run_id="run-001", service_id=service,
        query_template_id="qt-error-rate-v1", query_params={"service": service},
        raw_payload={"resultType": "matrix", "result": [
            {"metric": {"service": service},
             "values": [[int(at.timestamp()), "0.42"]]}]},
        observed_at=at,
        window=(at - timedelta(minutes=10), at + timedelta(minutes=10)),
    )


class TestRetentionPolicy:
    def test_default_policy_is_conservative(self):
        """默认保留 30 天，且要求事故已终结才删。"""
        policy = RetentionPolicy()
        assert policy.retain_days == 30
        assert policy.require_closed_incident is True

    def test_negative_retain_days_rejected(self):
        with pytest.raises(ValueError, match="retain_days"):
            RetentionPolicy(retain_days=-1)

    def test_zero_retain_days_allowed(self):
        """0 表示"只删已关闭事故的证据"（保守的最小策略）。"""
        assert RetentionPolicy(retain_days=0).retain_days == 0


class TestPurge:
    def test_old_evidence_from_closed_incident_is_purged(self, tmp_path: Path):
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        old = store.add(make_evidence(TS - timedelta(days=40)))
        policy = RetentionPolicy(retain_days=30)

        result = store.purge(
            policy=policy,
            now=TS,
            closed_incidents={"inc-001"},
        )

        assert result.purged == 1
        assert not store.exists(old.id)

    def test_recent_evidence_is_kept(self, tmp_path: Path):
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        recent = store.add(make_evidence(TS - timedelta(days=1)))
        policy = RetentionPolicy(retain_days=30)

        result = store.purge(policy=policy, now=TS, closed_incidents={"inc-001"})

        assert result.purged == 0
        assert store.exists(recent.id)

    def test_open_incident_evidence_is_never_purged(self, tmp_path: Path):
        """事故还没关闭时，即使证据很旧也不能删 —— 正在处理的事故需要它。"""
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        old = store.add(make_evidence(TS - timedelta(days=40)))
        policy = RetentionPolicy(retain_days=30, require_closed_incident=True)

        result = store.purge(policy=policy, now=TS, closed_incidents=set())

        assert result.purged == 0
        assert store.exists(old.id)

    def test_referenced_evidence_is_protected(self, tmp_path: Path):
        """被关系引用的证据不能删：删了关系就指向不存在的目标。"""
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        old = store.add(make_evidence(TS - timedelta(days=40), "redis"))
        other = store.add(make_evidence(TS - timedelta(days=40), "mysql"))
        store.add_relation(
            EvidenceRelation(source_evidence_id=old.id, target_evidence_id=other.id,
                             relation_type=EvidenceRelationType.PRECEDES,
                             time_delta_seconds=0.0),
            incident_id="inc-001")
        policy = RetentionPolicy(retain_days=30)

        result = store.purge(policy=policy, now=TS, closed_incidents={"inc-001"})

        assert store.exists(old.id), "evidence referenced by a relation must survive"
        assert store.exists(other.id)

    def test_purge_reports_protected_count(self, tmp_path: Path):
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        old = store.add(make_evidence(TS - timedelta(days=40), "redis"))
        free = store.add(make_evidence(TS - timedelta(days=40), "mysql"))
        store.add_relation(
            EvidenceRelation(source_evidence_id=old.id, target_evidence_id=free.id,
                             relation_type=EvidenceRelationType.PRECEDES,
                             time_delta_seconds=0.0),
            incident_id="inc-001")
        policy = RetentionPolicy(retain_days=30)

        result = store.purge(policy=policy, now=TS, closed_incidents={"inc-001"})

        assert result.protected == 2  # 两端都被关系引用
        assert result.purged == 0

    def test_in_memory_store_supports_purge(self):
        store = EvidenceStore()
        store.register_source("prometheus")
        old = store.add(make_evidence(TS - timedelta(days=40)))
        policy = RetentionPolicy(retain_days=30)
        result = store.purge(policy=policy, now=TS, closed_incidents={"inc-001"})
        assert result.purged == 1
        assert not store.exists(old.id)

    def test_dry_run_does_not_delete(self, tmp_path: Path):
        """先看清楚会删什么，再决定是否动手。"""
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        old = store.add(make_evidence(TS - timedelta(days=40)))
        policy = RetentionPolicy(retain_days=30)

        result = store.purge(policy=policy, now=TS, closed_incidents={"inc-001"}, dry_run=True)

        assert result.purged == 1  # 报告会说"会删 1 条"
        assert store.exists(old.id), "dry run must not delete"
