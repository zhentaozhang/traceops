package dev.traceops.controlplane.web;

import dev.traceops.controlplane.domain.IncidentStatus;

/** 接入结果：deduplicated=true 表示命中了未结束的同 fingerprint 事故。 */
public record IngestResult(String incidentId, IncidentStatus status, boolean deduplicated) {
}
