package dev.traceops.controlplane.web;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentOrigin;
import dev.traceops.controlplane.domain.IncidentStatus;
import java.time.Instant;

/** 事故的 REST 表示。 */
public record IncidentResponse(
        String id,
        String service,
        String title,
        String severity,
        IncidentStatus status,
        IncidentOrigin origin,
        Instant createdAt) {

    public static IncidentResponse from(Incident incident) {
        return new IncidentResponse(incident.getId(), incident.getService(),
                incident.getTitle(), incident.getSeverity(), incident.getStatus(),
                incident.getOrigin(), incident.getCreatedAt());
    }
}
