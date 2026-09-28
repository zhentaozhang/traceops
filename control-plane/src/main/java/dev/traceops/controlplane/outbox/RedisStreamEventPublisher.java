package dev.traceops.controlplane.outbox;

import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.redis.connection.RedisConnection;
import org.springframework.data.redis.core.RedisCallback;
import org.springframework.data.redis.connection.RedisStandaloneConfiguration;
import org.springframework.data.redis.connection.lettuce.LettuceConnectionFactory;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/**
 * Redis Streams 投递：XADD traceops.events * payload &lt;json&gt;。
 *
 * <p><b>必须校验 XADD 的返回值</b>：Spring Data 的
 * {@code opsForStream().add()} 在失败时可能返回 {@code null} 而**不抛异常**，
 * 于是 relay 会把它标记成"已发布" —— 事件永久丢失，而日志里什么都没有。
 *
 * <p>这不是理论风险，是实测到的：{@code publish()} 返回成功、零异常，
 * 但 Redis 的 {@code commandstats} 里**没有 xadd**，流长度为 0。
 * 接口报告成功而实际什么也没做，是比抛异常危险得多的失败模式。
 */
@Component
@ConditionalOnProperty(name = "traceops.events.redis.enabled", havingValue = "true")
public class RedisStreamEventPublisher implements EventPublisher {

    private static final Logger log = LoggerFactory.getLogger(RedisStreamEventPublisher.class);

    static final String STREAM_KEY = "traceops.events";

    /**
     * 流长度上限（近似裁剪）。
     *
     * <p>为什么必须有：Redis Stream 的 `XACK` **不删除数据**（只从 PEL 移除），
     * 所以不裁剪的流只涨不跌 —— 这是 Streams 最常见的内存泄漏。
     * 每个调查约 9 条事件，规模化后很容易到几十万条。
     *
     * <p>为什么是 10000：足够保留最近约 1000 次调查的完整事件用于排障，
     * 同时把内存钉在一个可预测的量级。需要更长重放时可以显式传更大值。
     */
    public static final int DEFAULT_MAX_LEN = 10_000;

    private final StringRedisTemplate redis;
    private final int maxLen;

    public RedisStreamEventPublisher(
            @org.springframework.beans.factory.annotation.Value("${spring.data.redis.host:localhost}") String host,
            @org.springframework.beans.factory.annotation.Value("${spring.data.redis.port:6379}") int port,
            @org.springframework.beans.factory.annotation.Value("${traceops.events.max-len:10000}") int maxLen) {
        // **自己装配而不是注入**：实测容器里存在一个未被 afterPropertiesSet()
        // 的 StringRedisTemplate，注入它会得到"template not initialized"，
        // 而它的失败是静默的（返回值 null、无异常）—— 事件就此丢失。
        // 显式构造让失败在启动期暴露，而不是等投递时静默丢数据。
        LettuceConnectionFactory factory = new LettuceConnectionFactory(
                new RedisStandaloneConfiguration(host, port));
        factory.afterPropertiesSet();
        StringRedisTemplate template = new StringRedisTemplate(factory);
        template.afterPropertiesSet();
        this.redis = template;
        this.maxLen = maxLen;
        log.info("RedisStreamEventPublisher initialised against {}:{} (maxLen={})",
                host, port, maxLen);
    }

    /** 配置的流长度上限；0 表示不裁剪（需显式选择）。 */
    public int maxLen() {
        return maxLen;
    }

    @Override
    public void publish(String message) {
        // 用底层命令而不是 opsForStream().add()：需要拿到真实的 XADD 回执
        // （形如 "1793439-0"），空回执就意味着**没写进去**。
        String id = redis.execute((RedisCallback<String>) (RedisConnection connection) -> {
            byte[] key = STREAM_KEY.getBytes(java.nio.charset.StandardCharsets.UTF_8);
            byte[] field = "payload".getBytes(java.nio.charset.StandardCharsets.UTF_8);
            byte[] value = message.getBytes(java.nio.charset.StandardCharsets.UTF_8);
            // MAXLEN ~ <n>：近似裁剪。精确形式每次插入都要重启计数并可能删多条，
            // 近似形式只在能整块丢弃 radix tree 节点时裁剪，成本低得多；
            // 代价是长度会略超上限，对"保留最近若干条"的用途完全可接受。
            // maxLen<=0 时退化为不裁剪（显式选择，不靠"忘了配"）。
            Object reply = maxLen > 0
                    ? connection.execute("XADD", key, "MAXLEN".getBytes(java.nio.charset.StandardCharsets.UTF_8),
                            "~".getBytes(java.nio.charset.StandardCharsets.UTF_8),
                            String.valueOf(maxLen).getBytes(java.nio.charset.StandardCharsets.UTF_8),
                            "*".getBytes(java.nio.charset.StandardCharsets.UTF_8), field, value)
                    : connection.execute("XADD", key,
                            "*".getBytes(java.nio.charset.StandardCharsets.UTF_8), field, value);
            if (reply == null) {
                return null;
            }
            return new String((byte[]) reply, java.nio.charset.StandardCharsets.UTF_8);
        });

        if (id == null || id.isBlank()) {
            // 没有回执 = 没写进去。必须抛，让 relay 标记失败并重试，
            // 绝不能让它被当作"已发布"而永久丢失。
            throw new IllegalStateException(
                    "XADD returned no id; the event was NOT published to " + STREAM_KEY);
        }
        log.debug("published to {} with id {}", STREAM_KEY, id);
    }
}
