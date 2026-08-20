"""Evidence Store + 确定性 Validators (blueprint §6.5, REVISIONS-1.1 R1)。

Store 职责:
- 持久化证据元数据。传入 `path` 时落 SQLite（标准库，无第三方依赖）；
  不传时保持纯内存（测试与单次运行）。**接口完全相同**，调用方无需改动。
- 持久化时计算 checksum（调用方不可指定）
- 四个确定性校验器: Provenance / Scope / Temporal / Reference(+checksum 完整性)
- 报告引用完整性检查: 报告中出现的 Evidence ID 必须真实存在
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from traceops.domain.models import Evidence, RCAReport
from traceops.evidence.retention import RetentionPolicy, RetentionResult


def compute_checksum(evidence: Evidence) -> str:
    """内容寻址 checksum: normalized_data 的规范 JSON 哈希（与 id/run_id 无关）。"""
    canonical = json.dumps(evidence.normalized_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ValidationResult:
    passed: bool
    checks: dict[str, str] = field(default_factory=dict)
    invalid_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    investigation_run_id TEXT NOT NULL,
    service_id TEXT,
    evidence_type TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    document TEXT NOT NULL,      -- Evidence 的完整 JSON（自描述，便于演进）
    normalized_data TEXT NOT NULL,  -- 单独一列，便于校验时重算 checksum 而不必反序列化整个文档
    raw_payload TEXT,           -- 原始数据源响应；P1 换成对象存储引用
    snapshot TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_incident ON evidence(incident_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(investigation_run_id);

CREATE TABLE IF NOT EXISTS evidence_relation (
    id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    source_evidence_id TEXT NOT NULL,
    target_evidence_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    document TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relation_incident ON evidence_relation(incident_id);
"""


class EvidenceStore:
    """证据存储。

    传 `path` 时用 SQLite 落盘（跨进程、跨重启可读）；不传时纯内存。
    两种模式的行为、不变量、校验语义完全一致。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._conn: sqlite3.Connection | None = None
        # 内存缓存：内存模式下就是唯一存储；持久化模式下避免每次 get 都反序列化
        self._evidence: dict[str, Evidence] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._sources: dict[str, bool] = {}  # source_id -> registered/allowed
        self._relations: list[Any] = []  # EvidenceRelation（P1 证据间关系）

        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path))
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- registry ----
    def register_source(self, source_id: str, allowed: bool = True) -> None:
        self._sources[source_id] = allowed

    # ---- persistence ----
    def add(self, evidence: Evidence) -> Evidence:
        evidence.checksum = compute_checksum(evidence)
        raw = evidence._raw_payload if evidence._raw_payload is not None else evidence.query_params
        snapshot = {
            "evidence_id": evidence.id,
            "checksum": evidence.checksum,
            "raw_payload": raw,  # P0: 随对象保存; P1: 写 MinIO/S3 并以 raw_snapshot_ref 引用
            "normalized": evidence.normalized_data,
        }
        self._evidence[evidence.id] = evidence
        self._snapshots[evidence.id] = snapshot

        if self._conn is not None:
            # 同一 id 重复写入按覆盖处理（upsert），不产生重复行
            self._conn.execute(
                """
                INSERT INTO evidence (id, incident_id, investigation_run_id, service_id,
                                      evidence_type, observed_at, checksum, schema_version,
                                      document, normalized_data, raw_payload, snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    document=excluded.document,
                    normalized_data=excluded.normalized_data,
                    raw_payload=excluded.raw_payload,
                    snapshot=excluded.snapshot,
                    checksum=excluded.checksum
                """,
                (
                    evidence.id, evidence.incident_id, evidence.investigation_run_id,
                    evidence.service_id, evidence.evidence_type.value,
                    evidence.observed_at.isoformat(), evidence.checksum,
                    evidence.schema_version,
                    evidence.model_dump_json(),
                    json.dumps(evidence.normalized_data, ensure_ascii=False, sort_keys=True),
                    json.dumps(raw, ensure_ascii=False, sort_keys=True) if raw is not None else None,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.commit()
        return evidence

    def add_relation(self, relation: Any, *, incident_id: str) -> Any:
        """写入一条证据间关系（P1）。

        与证据分开存放：关系是"关于证据的事实"，它的生命周期比某条证据长
        （证据可能过期被清理，但"这两条曾同属一条链路"仍应可查）。
        """
        self._relations.append(relation)
        if self._conn is not None:
            self._conn.execute(
                """
                INSERT INTO evidence_relation
                    (id, incident_id, source_evidence_id, target_evidence_id,
                     relation_type, document)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET document=excluded.document
                """,
                (
                    relation.id, incident_id,
                    relation.source_evidence_id, relation.target_evidence_id,
                    relation.relation_type.value,
                    relation.model_dump_json(),
                ),
            )
            self._conn.commit()
        return relation

    def list_relations(self, incident_id: str | None = None) -> list[Any]:
        if self._conn is not None:
            from traceops.domain.models import EvidenceRelation

            if incident_id is None:
                rows = self._conn.execute(
                    "SELECT document FROM evidence_relation ORDER BY id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT document FROM evidence_relation WHERE incident_id = ? ORDER BY id",
                    (incident_id,)).fetchall()
            return [EvidenceRelation.model_validate_json(row[0]) for row in rows]

        if incident_id is None:
            return list(self._relations)
        return [r for r in self._relations if getattr(r, "_incident_id", None) == incident_id]

    # ---- retention ----
    def purge(
        self,
        *,
        policy: RetentionPolicy,
        now: Any,
        closed_incidents: set[str],
        dry_run: bool = False,
    ) -> RetentionResult:
        """按策略清理陈旧证据。

        保护规则（见 retention.py 的模块文档）：
        - 事故未关闭 → 不删（即使超期）
        - 证据被任一关系引用 → 不删（否则关系悬空）
        """
        from datetime import timedelta

        result = RetentionResult(dry_run=dry_run)
        cutoff = now - timedelta(days=policy.retain_days)

        referenced: set[str] = set()
        for relation in self.list_relations():
            referenced.add(relation.source_evidence_id)
            referenced.add(relation.target_evidence_id)

        # 需要从磁盘读全部候选：内存模式与持久化模式都要能算
        candidates = self.list()

        for evidence in candidates:
            if evidence.observed_at >= cutoff:
                result.retained += 1
                continue
            if policy.require_closed_incident and evidence.incident_id not in closed_incidents:
                result.retained += 1
                continue
            if evidence.id in referenced:
                result.protected += 1
                continue

            result.purged += 1
            if dry_run:
                continue  # 只是报告会删什么
            self._delete(evidence.id)
        return result

    def _delete(self, evidence_id: str) -> None:
        self._evidence.pop(evidence_id, None)
        self._snapshots.pop(evidence_id, None)
        if self._conn is not None:
            self._conn.execute("DELETE FROM evidence WHERE id = ?", (evidence_id,))
            self._conn.commit()

    def _load_from_disk(self, evidence_id: str) -> Evidence | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT document, normalized_data, snapshot FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            return None
        document, normalized_json, snapshot_json = row
        evidence = Evidence.model_validate_json(document)
        # normalized_data 是独立一列，也是 checksum 的计算依据。
        # 从它读回（而不是从 document 读）才能发现"有人直接改了数据列"——
        # 如果两处都被改，checksum 仍会与记录值不符，篡改依旧暴露。
        if normalized_json is not None:
            evidence.normalized_data = json.loads(normalized_json)
        # 原始 payload 不在 document 里（PrivateAttr 不序列化），从快照列恢复
        if snapshot_json:
            snapshot = json.loads(snapshot_json)
            evidence._raw_payload = snapshot.get("raw_payload")
            self._snapshots[evidence_id] = snapshot
        self._evidence[evidence_id] = evidence
        return evidence

    def get(self, evidence_id: str) -> Evidence:
        cached = self._evidence.get(evidence_id)
        if cached is not None:
            return cached
        loaded = self._load_from_disk(evidence_id)
        if loaded is None:
            raise KeyError(evidence_id)
        return loaded

    def exists(self, evidence_id: str) -> bool:
        if evidence_id in self._evidence:
            return True
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        return row is not None

    def get_snapshot(self, evidence_id: str) -> dict[str, Any] | None:
        cached = self._snapshots.get(evidence_id)
        if cached is not None:
            return cached
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT snapshot FROM evidence WHERE id = ?", (evidence_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        snapshot = json.loads(row[0])
        self._snapshots[evidence_id] = snapshot
        return snapshot

    def list(
        self,
        incident_id: str | None = None,
        run_id: str | None = None,
        service_id: str | None = None,
    ) -> list[Evidence]:
        if self._conn is not None:
            clauses, params = [], []
            if incident_id is not None:
                clauses.append("incident_id = ?")
                params.append(incident_id)
            if run_id is not None:
                clauses.append("investigation_run_id = ?")
                params.append(run_id)
            if service_id is not None:
                clauses.append("service_id = ?")
                params.append(service_id)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = self._conn.execute(
                f"SELECT id FROM evidence{where} ORDER BY observed_at ASC, id ASC", params
            ).fetchall()
            return [self.get(row[0]) for row in rows]

        out = [
            e for e in self._evidence.values()
            if (incident_id is None or e.incident_id == incident_id)
            and (run_id is None or e.investigation_run_id == run_id)
            and (service_id is None or e.service_id == service_id)
        ]
        return sorted(out, key=lambda e: (e.observed_at, e.id))

    # ---- deterministic validators ----
    def validate(
        self,
        evidence_id: str,
        *,
        incident_id: str | None = None,
        investigation_window: tuple[Any, Any] | None = None,
    ) -> ValidationResult:
        try:
            ev = self.get(evidence_id)
        except KeyError:
            return ValidationResult(passed=False, errors=[f"evidence {evidence_id} not found"])

        checks: dict[str, str] = {}
        errors: list[str] = []

        # Provenance: 来源已注册
        registered = self._sources.get(ev.source_id)
        checks["provenance"] = "PASS" if registered else "FAIL"
        if not registered:
            errors.append(f"source {ev.source_id!r} not registered")

        # Checksum: 内容未被篡改
        current = compute_checksum(ev)
        checks["checksum"] = "PASS" if current == ev.checksum else "FAIL"
        if current != ev.checksum:
            errors.append("normalized data modified after persist")

        # Scope: 证据属于当前事故
        if incident_id is not None:
            checks["scope"] = "PASS" if ev.incident_id == incident_id else "FAIL"
            if ev.incident_id != incident_id:
                errors.append(f"evidence incident {ev.incident_id} != requested {incident_id}")

        # Temporal: 观测时间在调查窗口内
        if investigation_window is not None:
            w0, w1 = investigation_window
            inside = w0 <= ev.observed_at <= w1
            checks["temporal"] = "PASS" if inside else "FAIL"
            if not inside:
                errors.append(f"observed_at {ev.observed_at} outside investigation window")

        return ValidationResult(passed="FAIL" not in checks.values(), checks=checks, errors=errors)

    def validate_report_references(
        self,
        report: RCAReport,
        *,
        incident_id: str | None = None,
    ) -> ValidationResult:
        """报告引用完整性检查（成本低，P0 必做）:
        报告出现的每个 Evidence ID 必须存在于 Store，且（如指定）属于该事故。
        """
        referenced = report.all_evidence_ids()
        missing = sorted(eid for eid in referenced if not self.exists(eid))
        wrong_incident: list[str] = []
        if incident_id is not None:
            wrong_incident = sorted(
                eid for eid in referenced
                if self.exists(eid) and self.get(eid).incident_id != incident_id
            )
        passed = not missing and not wrong_incident
        errors = []
        if missing:
            errors.append(f"evidence references not found: {missing}")
        if wrong_incident:
            errors.append(f"evidence from other incidents: {wrong_incident}")
        return ValidationResult(passed=passed, invalid_ids=missing + wrong_incident, errors=errors)
