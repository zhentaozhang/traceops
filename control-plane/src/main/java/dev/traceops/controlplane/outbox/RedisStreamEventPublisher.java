package dev.traceops.controlplane.outbox;

import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

/** Redis Streams 投递：XADD traceops.events * payload <json>。 */
@Component
@ConditionalOnProperty(name = "traceops.events.redis.enabled", havingValue = "true")
public class RedisStreamEventPublisher implements EventPublisher {

    static final String STREAM_KEY = "traceops.events";

    private final StringRedisTemplate redis;

    public RedisStreamEventPublisher(StringRedisTemplate redis) {
        this.redis = redis;
    }

    @Override
    public void publish(String message) {
        redis.opsForStream().add(STREAM_KEY, Map.of("payload", message));
    }
}
