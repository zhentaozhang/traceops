package dev.traceops.controlplane.investigation;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Predicate;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.connection.stream.Consumer;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.connection.stream.ReadOffset;
import org.springframework.data.redis.connection.stream.StreamOffset;
import org.springframework.data.redis.connection.stream.StreamReadOptions;
import org.springframework.data.redis.core.StreamOperations;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/**
 * 生产实现：从 Redis Streams 的 consumer group 读取回程消息。
 *
 * <p>与写侧 {@code RedisStreamEventPublisher}（XADD {@code traceops.events * payload <json>}）对称：
 * 字段名同样是 {@code payload}，这样一条消息从 Java 发出、被 agent-runtime 以同样形状写回时，
 * 读侧不需要猜任何额外结构。
 *
 * <p>两个读法，合并在同一次 {@link #read()} 里：
 * <ol>
 *   <li><b>偏移 {@code 0}</b>——重放本消费者 PEL（pending entries）里尚未 ACK 的消息。
 *       这是 {@link InvestigationEventSource} 契约里"不 ACK = 下轮重试"真正生效的机制：
 *       {@code XREADGROUP >} 只会给新消息，任何"读了但没 ACK"的消息只能靠 PEL 重放再取回来。</li>
 *   <li><b>偏移 {@code >}</b>——读新消息。</li>
 * </ol>
 *
 * <p><b>consumer group 生命周期</b>：{@code XGROUP CREATE} 在 group 已存在时报 {@code BUSYGROUP}，
 * 报错不代表失败（多实例并发建组、或进程重启都是这个错误）；因此把 BUSYGROUP 视为"已就绪"，
 * 其余错误照抛——连不上 Redis 和"组已存在"必须区分，否则会把真正的故障吞掉。
 * 建组用 {@link ReadOffset#from(String) from("0")} 并从流头开始读：Control Plane 停机期间
 * 写进流里的调查结论不能丢（丢一条就等于把事故永久钉在 INVESTIGATING，而这正是本任务要修的洞）；
 * 从头重放是安全的，因为幂等账本（{@code applied_investigation_events}）会挡掉重复。
 */
@Component
public class RedisInvestigationEventSource implements InvestigationEventSource {

    private static final Logger log = LoggerFactory.getLogger(RedisInvestigationEventSource.class);

    private static final String BUSYGROUP = "BUSYGROUP";
    private static final String NOGROUP = "NOGROUP";

    private final StringRedisTemplate redis;
    private final String streamKey;
    private final String group;
    private final String consumerName;
    private final long batchSize;

    /**
     * 只在第一次（或 group 被外部销毁后）建组；稳态下每轮省掉一次到 Redis 的往返。
     * 用 AtomicBoolean 而不是 synchronized：这里只需要"最多失败后重试"的宽松语义，
     * 两个实例并发建组时第二个拿到 BUSYGROUP，本身也是安全的。
     */
    private final AtomicBoolean groupReady = new AtomicBoolean(false);

    public RedisInvestigationEventSource(
            StringRedisTemplate redis,
            @Value("${traceops.investigation.stream:traceops.events}") String streamKey,
            @Value("${traceops.investigation.consumer-group:control-plane-investigation}") String group,
            @Value("${traceops.investigation.consumer-name:control-plane-1}") String consumerName,
            @Value("${traceops.investigation.batch-size:10}") long batchSize) {
        this.redis = redis;
        this.streamKey = streamKey;
        this.group = group;
        this.consumerName = consumerName;
        this.batchSize = batchSize;
    }

    @Override
    public List<StreamMessage> read() {
        StreamOperations<String, String, String> ops = redis.opsForStream();
        try {
            return readGroup(ops);
        } catch (DataAccessException ex) {
            // 流被删除/重建（或 group 被运维销毁）后是 NOGROUP 而不是"没消息"。
            // 就地重建一次再读，而不是让故障持续到下一次进程重启。
            if (mentions(ex, NOGROUP)) {
                groupReady.set(false);
                ensureGroup(ops);
                return readGroup(ops);
            }
            throw ex;
        }
    }

    private List<StreamMessage> readGroup(StreamOperations<String, String, String> ops) {
        ensureGroup(ops);

        Consumer self = Consumer.from(group, consumerName);
        StreamReadOptions options = StreamReadOptions.empty().count(batchSize);

        // LinkedHashMap 去重：PEL 重放与新消息理论上不会重叠，但重复 id 会破坏"一条消息一次处理"，
        // 而且真出现重叠说明我们对 offset 的假设错了，先去重比重复驱动状态机安全。
        Map<String, StreamMessage> collected = new LinkedHashMap<>();
        collect(collected, ops.read(self, options, StreamOffset.create(streamKey, ReadOffset.from("0"))));
        collect(collected, ops.read(self, options, StreamOffset.create(streamKey, ReadOffset.lastConsumed())));
        return new ArrayList<>(collected.values());
    }

    private void collect(Map<String, StreamMessage> target,
            List<MapRecord<String, String, String>> records) {
        if (records == null) {
            // 没有新消息时驱动可能回 null（取决于客户端），统一按"空批"处理
            return;
        }
        for (MapRecord<String, String, String> record : records) {
            target.putIfAbsent(record.getId().getValue(), StreamMessage.of(record.getId().getValue(), record.getValue()));
        }
    }

    private void ensureGroup(StreamOperations<String, String, String> ops) {
        if (groupReady.get()) {
            return;
        }
        try {
            // Spring Data 的三参 createGroup 内部固定带 MKSTREAM：流还不存在时也会一并创建，
            // 否则首次启动（Java 还没发出任何事件）会因为流不存在而直接失败。
            ops.createGroup(streamKey, ReadOffset.from("0"), group);
            log.info("created investigation consumer group '{}' on stream '{}' (reading from the beginning)",
                    group, streamKey);
        } catch (DataAccessException ex) {
            if (!mentions(ex, BUSYGROUP)) {
                // 不是"已存在"，而是真故障（连不上、无权限）：交给调用方按读取失败计错误并下轮重试
                throw ex;
            }
            log.debug("investigation consumer group '{}' already exists on stream '{}'", group, streamKey);
        }
        groupReady.set(true);
    }

    @Override
    public void ack(String id) {
        // XACK 不存在的 id 返回 0，不抛异常；重复 ACK 因此天然幂等
        redis.opsForStream().acknowledge(streamKey, group, id);
    }

    /** 沿 cause 链找关键字：BUSYGROUP / NOGROUP 由 Redis 服务器拼在消息里，Spring 只做异常类型转换。 */
    private static boolean mentions(Throwable error, String keyword) {
        Predicate<String> contains = message -> message != null && message.contains(keyword);
        Throwable current = error;
        while (current != null) {
            if (contains.test(current.getMessage())) {
                return true;
            }
            current = current.getCause() == current ? null : current.getCause();
        }
        return false;
    }
}
