package dev.traceops.controlplane.outbox;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

/** 未配置消息队列时的兜底实现：只写日志，不做投递。 */
@Component
@ConditionalOnProperty(name = "traceops.events.redis.enabled", havingValue = "false", matchIfMissing = true)
public class LoggingEventPublisher implements EventPublisher {

    private static final Logger log = LoggerFactory.getLogger(LoggingEventPublisher.class);

    @Override
    public void publish(String message) {
        log.info("event published (log-only mode): {}", message);
    }
}
