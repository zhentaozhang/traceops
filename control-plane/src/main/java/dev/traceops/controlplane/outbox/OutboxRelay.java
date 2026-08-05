package dev.traceops.controlplane.outbox;

import java.time.Instant;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

/**
 * Outbox relay：按 created_at 顺序批量投递未发布事件。
 *
 * <p>语义：
 * <ul>
 *   <li>投递成功 → 标记 published_at，不会重发</li>
 *   <li>投递失败 → attempts+1、停止本轮（保序），下一轮重试</li>
 *   <li>至少一次语义，消费端必须按 event_id 幂等</li>
 * </ul>
 *
 * <p>本类不挂 {@code @Scheduled}：调度是另一个组件（{@link OutboxRelayScheduler}），
 * 这样"关掉定时调度"不会让 relayOnce 变成不可测试/不可调用的黑盒。
 */
@Component
public class OutboxRelay {

    private static final Logger log = LoggerFactory.getLogger(OutboxRelay.class);

    private final OutboxRepository repository;
    private final EventPublisher publisher;

    public OutboxRelay(OutboxRepository repository, EventPublisher publisher) {
        this.repository = repository;
        this.publisher = publisher;
    }

    @Transactional
    public int relayOnce() {
        List<OutboxEvent> batch = repository.findTop50ByPublishedAtIsNullOrderByCreatedAtAsc();
        int published = 0;
        for (OutboxEvent event : batch) {
            try {
                publisher.publish(event.getPayload());
                event.markPublished(Instant.now());
                repository.save(event);
                published++;
            } catch (Exception ex) {
                event.markFailed();
                repository.save(event);
                log.warn("outbox publish failed for {} (attempt {}): {}",
                        event.getEventId(), event.getAttempts(), ex.toString());
                break;
            }
        }
        return published;
    }
}
