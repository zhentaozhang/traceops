package dev.traceops.controlplane.investigation;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;
import java.time.Instant;

/**
 * 已应用的调查终态事件：消费端幂等账本。
 *
 * <p>为什么把 {@code event_id} 直接作为主键（而不是自增 ID + 唯一约束）：
 * 幂等语义要求"同一 event_id 至多一条记录"，主键正是数据库层面唯一性最强的表达，
 * 而且 {@code save} 命中已有主键时天然就是"冲突"而不是"新增一条"。这样即便
 * 两个消费者并发处理同一个事件，第二个在 flush 时会被主键冲突拦下（抛异常/回滚），
 * 而不是静默写入两条、把状态机驱动两次。
 *
 * <p>为什么还要 {@code investigation_run_id} 的唯一约束：契约把 completed/failed 的
 * 幂等键定义为 {@code event_id}，但同一个 Run 重复回程（例如上游重发时重新生成了
 * event_id）在语义上仍是同一次调查，不能让状态机被驱动两次。两个键分别在
 * {@link #eventId} 主键与 {@link #investigationRunId} 唯一约束上落地，先查后写；
 * 数据库约束是最后一道防线（并发窗口），Java 侧的预检才是常规路径。
 */
@Entity
@Table(name = "applied_investigation_events",
        uniqueConstraints = @UniqueConstraint(name = "uk_applied_run_id", columnNames = "investigationRunId"))
public class AppliedInvestigationEvent {

    @Id
    private String eventId;

    @Column(nullable = false)
    private String investigationRunId;

    @Column(nullable = false)
    private String eventType;

    private String incidentId;

    /** 处理结论（APPLIED / NEEDS_HUMAN_REVIEW / REJECTED / ...），用于事后审账。 */
    @Column(nullable = false)
    private String outcome;

    private String previousStatus;

    private String resultingStatus;

    /** 被拒原因或人工复核原因；成功推进时可为 null。 */
    @Column(length = 500)
    private String reason;

    @Column(nullable = false)
    private Instant appliedAt;

    protected AppliedInvestigationEvent() {
    }

    private AppliedInvestigationEvent(String eventId, String investigationRunId, String eventType, String incidentId,
            String outcome, String previousStatus, String resultingStatus, String reason, Instant appliedAt) {
        this.eventId = eventId;
        this.investigationRunId = investigationRunId;
        this.eventType = eventType;
        this.incidentId = incidentId;
        this.outcome = outcome;
        this.previousStatus = previousStatus;
        this.resultingStatus = resultingStatus;
        this.reason = reason;
        this.appliedAt = appliedAt;
    }

    public static AppliedInvestigationEvent of(String eventId, String investigationRunId, String eventType,
            String incidentId, String outcome, String previousStatus, String resultingStatus, String reason,
            Instant appliedAt) {
        return new AppliedInvestigationEvent(eventId, investigationRunId, eventType, incidentId, outcome,
                previousStatus, resultingStatus, reason, appliedAt);
    }

    public String getEventId() {
        return eventId;
    }

    public String getInvestigationRunId() {
        return investigationRunId;
    }

    public String getEventType() {
        return eventType;
    }

    public String getIncidentId() {
        return incidentId;
    }

    public String getOutcome() {
        return outcome;
    }

    public String getPreviousStatus() {
        return previousStatus;
    }

    public String getResultingStatus() {
        return resultingStatus;
    }

    public String getReason() {
        return reason;
    }

    public Instant getAppliedAt() {
        return appliedAt;
    }
}
