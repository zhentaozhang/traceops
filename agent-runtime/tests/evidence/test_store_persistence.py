"""Evidence Store 持久化测试（把内存实现换成可落盘实现）。

## 为什么要持久化

调查进程崩溃、或需要跨进程审计时，内存里的证据会全部消失。
证据是"可追溯的事实记录"，丢证据等于整个系统的可信性归零。

## 设计约束

- 接口完全不变（EvidenceStore 的调用方不需要改一行代码）
- 落盘格式必须自描述：带 schema_version，未来扩展可读旧数据
- **checksum 必须重算并比对**：从磁盘读回的证据若被篡改，必须能被发现
- 原始 payload 也落盘（P1 会换成对象存储 + raw_snapshot_ref，接口不变）

用 SQLite（标准库），不引入数据库依赖。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from traceops.domain.models import Evidence, EvidenceType
from traceops.evidence.normalizer import normalize
from traceops.evidence.store import EvidenceStore, compute_checksum

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW = (TS - timedelta(minutes=10), TS + timedelta(minutes=10))


def make_evidence(service: str = "redis", *, run_id: str = "run-001") -> Evidence:
    return normalize(
        evidence_type=EvidenceType.METRIC, source_id="prometheus",
        incident_id="inc-001", run_id=run_id, service_id=service,
        query_template_id="qt-error-rate-v1", query_params={"service": service},
        raw_payload={"resultType": "matrix", "result": [
            {"metric": {"service": service}, "values": [[int(TS.timestamp()), "0.42"]]}]},
        observed_at=TS, window=WINDOW,
    )


class TestPersistence:
    def test_evidence_survives_store_restart(self, tmp_path: Path):
        """核心断言：换一个实例读同一个文件，证据还在。"""
        db = tmp_path / "evidence.db"

        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        evidence = store.add(make_evidence())
        evidence_id = evidence.id

        # 模拟进程重启：全新实例，同一个文件
        reopened = EvidenceStore(path=db)
        reopened.register_source("prometheus")
        loaded = reopened.get(evidence_id)

        assert loaded.id == evidence_id
        assert loaded.service_id == "redis"
        assert loaded.normalized_data == evidence.normalized_data
        assert loaded.checksum == evidence.checksum

    def test_checksum_recomputed_and_matches_on_load(self, tmp_path: Path):
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        evidence = store.add(make_evidence())

        reopened = EvidenceStore(path=db)
        reopened.register_source("prometheus")
        loaded = reopened.get(evidence.id)

        # 从磁盘读回后必须重算 checksum 并与此前一致
        assert compute_checksum(loaded) == loaded.checksum

    def test_tampered_database_row_is_detected(self, tmp_path: Path):
        """数据库被手改（或存储损坏）时，validate 必须报 FAIL。"""
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        evidence = store.add(make_evidence())

        # 直接篡改 SQLite 里的 normalized_data
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE evidence SET normalized_data = ? WHERE id = ?",
            (json.dumps({"kind": "metric", "series": [], "result_count": 0}),
             evidence.id),
        )
        conn.commit()
        conn.close()

        reopened = EvidenceStore(path=db)
        reopened.register_source("prometheus")
        result = reopened.validate(evidence.id)

        assert not result.passed
        assert result.checks["checksum"] == "FAIL"

    def test_raw_payload_persisted(self, tmp_path: Path):
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        evidence = store.add(make_evidence())

        reopened = EvidenceStore(path=db)
        snapshot = reopened.get_snapshot(evidence.id)
        assert snapshot is not None
        assert snapshot["raw_payload"]["resultType"] == "matrix"

    def test_list_filters_work_after_reload(self, tmp_path: Path):
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        first = store.add(make_evidence("redis", run_id="run-001"))
        second = store.add(make_evidence("mysql", run_id="run-002"))

        reopened = EvidenceStore(path=db)
        run1 = reopened.list(run_id="run-001")
        assert [e.id for e in run1] == [first.id]
        all_inc = reopened.list(incident_id="inc-001")
        assert {first.id, second.id} <= {e.id for e in all_inc}

    def test_schema_version_persisted(self, tmp_path: Path):
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        evidence = store.add(make_evidence())

        reopened = EvidenceStore(path=db)
        assert reopened.get(evidence.id).schema_version == "1.0"


class TestBackwardCompatibility:
    def test_path_none_keeps_in_memory_behaviour(self):
        """不传 path 时行为与之前完全一致（纯内存）。"""
        store = EvidenceStore()
        store.register_source("prometheus")
        evidence = store.add(make_evidence())
        assert store.get(evidence.id).id == evidence.id

    def test_missing_file_is_created(self, tmp_path: Path):
        db = tmp_path / "nested" / "evidence.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        store.add(make_evidence())
        assert db.exists()

    def test_duplicate_id_overwrites_not_duplicates(self, tmp_path: Path):
        db = tmp_path / "evidence.db"
        store = EvidenceStore(path=db)
        store.register_source("prometheus")
        evidence = store.add(make_evidence())
        store.add(evidence)  # 同一 id 再写一次

        reopened = EvidenceStore(path=db)
        matching = [e for e in reopened.list(incident_id="inc-001") if e.id == evidence.id]
        assert len(matching) == 1, "same evidence id must not be duplicated"


class TestConcurrentSafety:
    def test_two_stores_on_same_file_see_each_others_writes(self, tmp_path: Path):
        """两个实例（模拟两个 worker）写同一文件，彼此能看到对方的数据。"""
        db = tmp_path / "evidence.db"
        a = EvidenceStore(path=db)
        a.register_source("prometheus")
        evidence_a = a.add(make_evidence("redis", run_id="run-a"))

        b = EvidenceStore(path=db)
        b.register_source("prometheus")
        evidence_b = b.add(make_evidence("mysql", run_id="run-b"))

        # 都从磁盘重新读，应看到两条
        fresh = EvidenceStore(path=db)
        ids = {e.id for e in fresh.list(incident_id="inc-001")}
        assert {evidence_a.id, evidence_b.id} <= ids
