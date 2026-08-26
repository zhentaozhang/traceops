package dev.traceops.controlplane.investigation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.traceops.controlplane.events.EventMessages;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Component;

/**
 * 回程闭环的最后一段：从消息源拉取调查终态事件，交给 {@link InvestigationResultService} 结算，
 * 再按结算结论决定 ACK 与否。
 *
 * <p>为什么这个类是普通 {@code @Component} 而不带 {@code @Scheduled}：调度由
 * {@link InvestigationEventPollerScheduler} 单独承担。这不是风格洁癖——曾经把
 * {@code @Scheduled} 与 {@code @ConditionalOnProperty} 放在同一个类里，开关一关整个 bean 消失，
 * 于是"核心逻辑"没有可调用入口，也没法在测试里直接调 {@link #pollOnce()}。
 * 沿用 {@code OutboxRelay} / {@code OutboxRelayScheduler} 的拆分。
 *
 * <p>ACK 的判据只有一条：<b>这条消息是否已经"结算完毕"</b>。
 * 结算完毕 = 幂等账本里要么已经有一条结论记录、要么确实没有对应事故可以结算。
 * 只要还可能有"下一轮就能成功"的进展，就不 ACK，让它留在 PEL 里重投：
 * <ul>
 *   <li>{@code NOT_FOUND} 不 ACK——事故落库可能比回程消息晚（跨服务乱序），下一轮可能就有了。
 *       ACK 会把这条结论永久丢掉，事故再也不会被推进。</li>
 *   <li>暂时性异常（数据库不可用等）不 ACK——重投才是正确的恢复手段。</li>
 *   <li>契约违约 / 坏 JSON / 未知事件类型 ACK——重投一万次也一样坏，留在 PEL 只会堵塞消费者；
 *       这类消息的意义是"计数 + 日志告警"，不是重试。</li>
 * </ul>
 */
@Component
public class InvestigationEventPoller {

    private static final Logger log = LoggerFactory.getLogger(InvestigationEventPoller.class);

    private final InvestigationResultService service;
    private final InvestigationEventSource source;
    private final ObjectMapper mapper;

    public InvestigationEventPoller(InvestigationResultService service,
            InvestigationEventSource source, ObjectMapper mapper) {
        this.service = service;
        this.source = source;
        this.mapper = mapper;
    }

    /**
     * 拉取并处理一批消息；永不抛出（调度线程不能因为一次 Redis 抖动而停摆）。
     * 返回本轮计数，供指标/日志消费。
     */
    public PollResult pollOnce() {
        List<StreamMessage> batch;
        try {
            batch = source.read();
        } catch (RuntimeException ex) {
            // Redis 不可用：本轮什么都没做。不抛，因为抛出只会让 @Scheduled 线程打一行堆栈，
            // 下一轮仍然要重试；计数让监控侧看到"读取失败"这个独立信号。
            log.warn("investigation event poll failed to read from the event source: {}", ex.toString());
            return PollResult.readFailure();
        }

        Counters counters = new Counters();
        for (StreamMessage message : batch) {
            counters.processed++;
            handle(message, counters);
        }

        PollResult result = counters.toResult();
        if (result.errors() > 0 || result.notFound() > 0) {
            log.warn("investigation poll finished with problems: {}", result);
        } else if (result.processed() > 0) {
            log.debug("investigation poll finished: {}", result);
        }
        return result;
    }

    private void handle(StreamMessage message, Counters counters) {
        String rawJson = message.payload();
        String eventType = peekEventType(rawJson);

        if (EventMessages.INVESTIGATION_REQUESTED.equals(eventType)) {
            // Java 自己发出的调查请求也会出现在同一个流里。消费自己的请求会形成自激循环
            // （并且 apply 一定因 producer/ event_type 校验失败而报错），因此这里必须显式跳过。
            // 仍然 ACK：它不是"待重试"，只是不该由本消费者处理，留在 PEL 里没有意义。
            log.debug("skipping investigation.requested written by the control plane itself: id={}", message.id());
            counters.skipped++;
            ack(message);
            return;
        }

        try {
            Applied applied = service.apply(rawJson);
            countOutcome(applied, message, counters);
        } catch (DataIntegrityViolationException ex) {
            // 并发窗口：另一个消费者已经写入同一 event_id（主键）或同一 run_id（唯一约束）。
            // 这是幂等语义的"成功"分支而不是错误——状态机只被驱动了一次，本线程只是后到。
            // 若把它当错误并重投，会反复撞约束；当异常不 ACK，则每次重投都失败一次。
            log.debug("investigation event raced with another consumer and lost the unique constraint"
                    + " (treated as idempotent success): id={}", message.id());
            counters.skipped++;
            ack(message);
        } catch (IllegalArgumentException ex) {
            // 契约违约（缺字段、producer/event_type 不符）或坏 JSON：重投不可能变好。
            // ACK 掉并计数，把问题留给日志与告警，而不是让一条毒消息永久占住 PEL。
            log.warn("discarding unusable investigation message {}: {}", message.id(), ex.getMessage());
            counters.errors++;
            ack(message);
        } catch (RuntimeException ex) {
            // 其余都是暂时性的（数据库/连接问题）：不 ACK，让它留在 PEL，下一轮重投。
            // 这里不 ACK 是唯一能恢复的选择——ACK 等于确认"处理完成"，而它并没有被处理。
            log.warn("investigation message {} failed transiently, leaving it pending for retry: {}",
                    message.id(), ex.toString());
            counters.errors++;
        }
    }

    /** 结算结论 → 计数 + ACK 决策。这里就是"什么算处理完"的唯一一处定义。 */
    private void countOutcome(Applied applied, StreamMessage message, Counters counters) {
        switch (applied.outcome()) {
            case APPLIED -> {
                counters.applied++;
                log.info("investigation result applied: incident={} {} -> {} (event_id={})",
                        applied.incidentId(), applied.previousStatus(), applied.resultingStatus(),
                        applied.eventId());
                ack(message);
            }
            case SKIPPED_DUPLICATE -> {
                // 至少一次语义下的正常现象，不是异常：账本已经结算过这一条
                counters.skipped++;
                log.debug("duplicate investigation event ignored: event_id={} reason={}",
                        applied.eventId(), applied.reason());
                ack(message);
            }
            case NEEDS_HUMAN_REVIEW -> {
                counters.needsReview++;
                // 需要人工不等于"消息没处理完"：结论已入账，状态机已按语义停住。
                // 不 ACK 只会让它反复重投，而重投的结果仍然是 NEEDS_HUMAN_REVIEW。
                log.info("investigation result needs human review: incident={} reason={}",
                        applied.incidentId(), applied.reason());
                ack(message);
            }
            case RETRY_PENDING -> {
                counters.retryPending++;
                log.info("retryable investigation failure recorded: incident={} reason={}",
                        applied.incidentId(), applied.reason());
                ack(message);
            }
            case REJECTED -> {
                counters.rejected++;
                // 状态机拒绝（陈旧消息撞上已推进/已解决的事故）：已记账，重投没有意义
                log.info("stale investigation result rejected: event_id={} reason={}",
                        applied.eventId(), applied.reason());
                ack(message);
            }
            case NOT_FOUND -> {
                counters.notFound++;
                // 故意不 ACK：事故可能只是还没落库。留待下轮重试，才是"闭环"该有的韧性。
                log.warn("investigation result references an unknown incident, leaving it pending for retry:"
                        + " event_id={} incident_id={}", applied.eventId(), applied.incidentId());
            }
        }
    }

    private void ack(StreamMessage message) {
        try {
            source.ack(message.id());
        } catch (RuntimeException ex) {
            // ACK 失败时消息会在下一轮重新出现；幂等账本保证那一次是 SKIPPED_DUPLICATE。
            // 因此这里记录而不中断整批——不能因为一条消息的 ACK 失败就丢掉后面所有消息。
            log.warn("failed to ACK investigation message {}; it will be redelivered and deduplicated: {}",
                    message.id(), ex.toString());
        }
    }

    /**
     * 只读信封里的 event_type，用于在进入业务处理前分流。
     *
     * <p>刻意只读到这一层：payload 的结构解析归 {@link InvestigationResult}，身份校验归
     * {@link InvestigationResultService}。这里读不出来（坏 JSON / 缺 envelope）就原样交给 service，
     * 由它按契约报错，避免这里出现第二套（容易和契约漂移的）校验。
     */
    private String peekEventType(String rawJson) {
        if (rawJson == null || rawJson.isBlank()) {
            return null;
        }
        try {
            JsonNode envelope = mapper.readTree(rawJson).path("envelope");
            if (!envelope.isObject()) {
                return null;
            }
            JsonNode eventType = envelope.path("event_type");
            return eventType.isTextual() && !eventType.asText().isBlank() ? eventType.asText() : null;
        } catch (Exception ex) {
            return null;
        }
    }

    /** 可变计数器：让 {@link #handle} 保持无返回值、单一职责。 */
    private static final class Counters {
        private int processed;
        private int applied;
        private int skipped;
        private int needsReview;
        private int retryPending;
        private int rejected;
        private int notFound;
        private int errors;

        private PollResult toResult() {
            return new PollResult(processed, applied, skipped, needsReview, retryPending, rejected,
                    notFound, errors);
        }
    }
}
