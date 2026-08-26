package dev.traceops.controlplane.investigation;

import java.util.Map;

/**
 * 从 Redis Stream 读到的一条消息（读侧的抽象）。
 *
 * <p>为什么要这层 record 而不是直接用 {@code MapRecord}：让 {@link InvestigationEventPoller}
 * 的"判断该不该处理、该不该 ACK"这段核心逻辑与 Spring Data Redis 的具体类型解耦。
 * 生产实现 {@code RedisInvestigationEventSource} 负责 XADD/XREADGROUP 的形状，
 * 测试用内存实现即可完整覆盖 poller，不需要一个真实 Redis。
 *
 * @param id     Stream 内的消息 ID（ACK 的凭证，XACK 只认这个）
 * @param fields Stream 条目字段（Java 侧写的字段名是 {@code payload}）
 */
public record StreamMessage(String id, Map<String, String> fields) {

    /** 事件信封里承载整条消息 JSON 的字段名，与 {@code RedisStreamEventPublisher} 的写侧对称。 */
    public static final String PAYLOAD_FIELD = "payload";

    public StreamMessage {
        fields = fields == null ? Map.of() : Map.copyOf(fields);
    }

    public static StreamMessage of(String id, Map<String, String> fields) {
        return new StreamMessage(id, fields);
    }

    /** 整条消息的 JSON；字段缺失时返回 null，由调用方按"坏消息"处理。 */
    public String payload() {
        return fields.get(PAYLOAD_FIELD);
    }
}
