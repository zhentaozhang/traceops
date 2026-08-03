"""Evidence Store + 确定性 Validators (blueprint §6.5, REVISIONS-1.1 R1)。

Store 职责:
- 持久化证据元数据（P0 内存实现；P1 换 PostgreSQL + MinIO 快照，接口不变）
- 持久化时计算 checksum（调用方不可指定）
- 四个确定性校验器: Provenance / Scope / Temporal / Reference(+checksum 完整性)
- 报告引用完整性检查: 报告中出现的 Evidence ID 必须真实存在
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from traceops.domain.models import Evidence, RCAReport


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


class EvidenceStore:
    """P0 内存实现。生产版实现同一接口，后端替换为 PostgreSQL/对象存储。"""

    def __init__(self) -> None:
        self._evidence: dict[str, Evidence] = {}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._sources: dict[str, bool] = {}  # source_id -> registered/allowed

    # ---- registry ----
    def register_source(self, source_id: str, allowed: bool = True) -> None:
        self._sources[source_id] = allowed

    # ---- persistence ----
    def add(self, evidence: Evidence) -> Evidence:
        evidence.checksum = compute_checksum(evidence)
        self._evidence[evidence.id] = evidence
        raw = evidence._raw_payload if evidence._raw_payload is not None else evidence.query_params
        self._snapshots[evidence.id] = {
            "evidence_id": evidence.id,
            "checksum": evidence.checksum,
            "raw_payload": raw,  # P0: 随对象保存; P1: 写 MinIO/S3 并以 raw_snapshot_ref 引用
            "normalized": evidence.normalized_data,
        }
        return evidence

    def get(self, evidence_id: str) -> Evidence:
        return self._evidence[evidence_id]

    def exists(self, evidence_id: str) -> bool:
        return evidence_id in self._evidence

    def get_snapshot(self, evidence_id: str) -> dict[str, Any] | None:
        return self._snapshots.get(evidence_id)

    def list(
        self,
        incident_id: str | None = None,
        run_id: str | None = None,
        service_id: str | None = None,
    ) -> list[Evidence]:
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
            ev = self._evidence[evidence_id]
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
