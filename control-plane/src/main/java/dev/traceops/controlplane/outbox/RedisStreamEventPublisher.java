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

    private final StringRedisTemplate redis;

    public RedisStreamEventPublisher(
            @org.springframework.beans.factory.annotation.Value("${spring.data.redis.host:localhost}") String host,
            @org.springframework.beans.factory.annotation.Value("${spring.data.redis.port:6379}") int port) {
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
        log.info("RedisStreamEventPublisher initialised against {}:{}", host, port);
    }

    @Override
    public void publish(String message) {
        // 用底层命令而不是 opsForStream().add()：需要拿到真实的 XADD 回执
        // （形如 "1793439-0"），空回执就意味着**没写进去**。
        String id = redis.execute((RedisCallback<String>) (RedisConnection connection) -> {
            byte[] key = STREAM_KEY.getBytes(java.nio.charset.StandardCharsets.UTF_8);
            byte[] field = "payload".getBytes(java.nio.charset.StandardCharsets.UTF_8);
            byte[] value = message.getBytes(java.nio.charset.StandardCharsets.UTF_8);
            Object reply = connection.execute("XADD", key, "*".getBytes(java.nio.charset.StandardCharsets.UTF_8),
                    field, value);
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
