package dev.traceops.controlplane.web;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentStatus;
import java.time.Instant;

public record IncidentResponse(
        String id,
        String service,
        String title,
        String severity,
        IncidentStatus status,
        Instant createdAt,
        Instant updatedAt,
        long version) {

    public static IncidentResponse from(Incident incident) {
        return new IncidentResponse(incident.getId(), incident.getService(), incident.getTitle(),
                incident.getSeverity(), incident.getStatus(), incident.getCreatedAt(),
                incident.getUpdatedAt(), incident.getVersion());
    }
}
