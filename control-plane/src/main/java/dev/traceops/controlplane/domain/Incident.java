package dev.traceops.controlplane.domain;

import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.Instant;

/** 一次告警对应的事故聚合根。状态只能经 {@link #transitionTo} 变更。 */
@Entity
@Table(name = "incidents")
public class Incident {

    @Id
    private String id;

    private String fingerprint;

    private String service;

    private String title;

    private String severity;

    @Enumerated(EnumType.STRING)
    private IncidentStatus status;

    private Instant createdAt;

    private Instant updatedAt;

    @Version
    private long version;

    protected Incident() {
    }

    private Incident(String id, String fingerprint, String service, String title, String severity, Instant now) {
        this.id = id;
        this.fingerprint = fingerprint;
        this.service = service;
        this.title = title;
        this.severity = severity;
        this.status = IncidentStatus.NEW;
        this.createdAt = now;
        this.updatedAt = now;
    }

    public static Incident open(String id, String fingerprint, String service, String title, String severity, Instant now) {
        return new Incident(id, fingerprint, service, title, severity, now);
    }

    public void transitionTo(IncidentStatus target, Instant now) {
        if (target == null) {
            throw new IllegalArgumentException("target status is required");
        }
        if (!status.canTransitionTo(target)) {
            throw new IllegalTransitionException(id, status, target);
        }
        this.status = target;
        this.updatedAt = now;
    }

    public String getId() {
        return id;
    }

    public String getFingerprint() {
        return fingerprint;
    }

    public String getService() {
        return service;
    }

    public String getTitle() {
        return title;
    }

    public String getSeverity() {
        return severity;
    }

    public IncidentStatus getStatus() {
        return status;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public Instant getUpdatedAt() {
        return updatedAt;
    }

    public long getVersion() {
        return version;
    }
}
