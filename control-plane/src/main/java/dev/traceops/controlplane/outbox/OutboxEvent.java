package dev.traceops.controlplane.outbox;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Lob;
import jakarta.persistence.Table;
import java.time.Instant;

/**
 * Transactional Outbox 行：业务事务内落库，由 relay 异步投递。
 *
 * <p>不直接发消息的原因：跨 DB + 消息队列的两阶段提交不可靠；
 * outbox 让"写事故"和"要发事件"在同一个本地事务里成功或失败。
 */
@Entity
@Table(name = "outbox_events")
public class OutboxEvent {

    @Id
    private String eventId;

    private String eventType;

    private String aggregateId;

    private long aggregateVersion;

    @Lob
    @Column(nullable = false)
    private String payload;

    private Instant createdAt;

    private Instant publishedAt;

    private int attempts;

    protected OutboxEvent() {
    }

    private OutboxEvent(String eventId, String eventType, String aggregateId, long aggregateVersion,
            String payload, Instant createdAt) {
        this.eventId = eventId;
        this.eventType = eventType;
        this.aggregateId = aggregateId;
        this.aggregateVersion = aggregateVersion;
        this.payload = payload;
        this.createdAt = createdAt;
        this.attempts = 0;
    }

    public static OutboxEvent pending(String eventId, String eventType, String aggregateId,
            long aggregateVersion, String payload, Instant now) {
        return new OutboxEvent(eventId, eventType, aggregateId, aggregateVersion, payload, now);
    }

    public void markPublished(Instant now) {
        this.publishedAt = now;
    }

    public void markFailed() {
        this.attempts += 1;
    }

    public boolean isPublished() {
        return publishedAt != null;
    }

    public String getEventId() {
        return eventId;
    }

    public String getEventType() {
        return eventType;
    }

    public String getAggregateId() {
        return aggregateId;
    }

    public long getAggregateVersion() {
        return aggregateVersion;
    }

    public String getPayload() {
        return payload;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public Instant getPublishedAt() {
        return publishedAt;
    }

    public int getAttempts() {
        return attempts;
    }
}
