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

    /**
     * 最早允许投递的时刻。{@code null} 表示"立即投递"。
     *
     * <p>为什么需要它：退避。失败往往来自下游抖动（MCP 挂了、模型限流），
     * 立刻重试只是立刻再撞一次同样的墙，还把失败放大成风暴。
     * 把"最早什么时候可以发"和"什么时候创建的"分开，退避才有落脚点 ——
     * 否则 {@code createdAt} 既表示创建时间又表示可投递时间，诊断时看到的含义会自相矛盾。
     */
    private Instant notBefore;

    private Instant publishedAt;

    private int attempts;

    protected OutboxEvent() {
    }

    private OutboxEvent(String eventId, String eventType, String aggregateId, long aggregateVersion,
            String payload, Instant createdAt, Instant notBefore) {
        this.eventId = eventId;
        this.eventType = eventType;
        this.aggregateId = aggregateId;
        this.aggregateVersion = aggregateVersion;
        this.payload = payload;
        this.createdAt = createdAt;
        this.notBefore = notBefore;
        this.attempts = 0;
    }

    public static OutboxEvent pending(String eventId, String eventType, String aggregateId,
            long aggregateVersion, String payload, Instant now) {
        return new OutboxEvent(eventId, eventType, aggregateId, aggregateVersion, payload, now, null);
    }

    /**
     * 一条**带退避**的待投递事件：{@code notBefore} 之前不得投递。
     *
     * <p>用于重试：失败后不要马上再撞一次同一个下游。
     */
    public static OutboxEvent pendingAfter(String eventId, String eventType, String aggregateId,
            long aggregateVersion, String payload, Instant now, Instant notBefore) {
        return new OutboxEvent(eventId, eventType, aggregateId, aggregateVersion, payload, now, notBefore);
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

    /** 最早允许投递的时刻；{@code null} 表示立即可投递。 */
    public Instant getNotBefore() {
        return notBefore;
    }

    /** 当前时刻是否已经可以投递。 */
    public boolean isDeliverableAt(Instant now) {
        return notBefore == null || !now.isBefore(notBefore);
    }

    public Instant getPublishedAt() {
        return publishedAt;
    }

    public int getAttempts() {
        return attempts;
    }
}
