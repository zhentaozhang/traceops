package dev.traceops.controlplane.web;

import dev.traceops.controlplane.domain.IncidentOrigin;
import dev.traceops.controlplane.domain.IncidentStatus;

/** 人工创建事故的响应。 */
public record ManualIncidentResult(
        String incidentId,
        IncidentStatus status,
        IncidentOrigin origin,
        String investigationRunId) {
}
