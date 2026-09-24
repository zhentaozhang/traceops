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

    /**
     * 告警指纹。**人工创建的事故没有指纹**。
     *
     * <p>为什么允许为空而不是塞一个假值：去重逻辑依赖它，
     * 假指纹会让两条独立的人工事故互相"去重"而只留一条。
     */
    private String fingerprint;

    private String service;

    private String title;

    private String severity;

    /**
     * 事故来源：ALERT（告警驱动）/ MANUAL（人工创建）。
     *
     * <p>必须持久化：事后复盘要能区分"这次是告警触发的"还是"SRE 手工立的"，
     * 而且去重查询必须**排除** MANUAL —— 人工创建是主动行为，不该被指纹规则吞掉。
     */
    @Enumerated(EnumType.STRING)
    private IncidentOrigin origin = IncidentOrigin.ALERT;

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

    /**
     * 人工创建事故。
     *
     * <p>刻意**不生成 fingerprint**：告警去重靠它，人工创建没有告警。
     * 塞一个假指纹会让两条独立的人工事故互相去重。
     */
    public static Incident openManual(String id, String service, String title, String severity, Instant now) {
        Incident incident = new Incident(id, null, service, title, severity, now);
        incident.origin = IncidentOrigin.MANUAL;
        return incident;
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

    public IncidentOrigin getOrigin() {
        return origin;
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
