package dev.traceops.controlplane.outbox;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/** 只负责定时触发 {@link OutboxRelay#relayOnce()}；可用 traceops.outbox.relay-enabled=false 关闭。 */
@Component
@ConditionalOnProperty(name = "traceops.outbox.relay-enabled", havingValue = "true", matchIfMissing = true)
public class OutboxRelayScheduler {

    private final OutboxRelay relay;

    public OutboxRelayScheduler(OutboxRelay relay) {
        this.relay = relay;
    }

    @Scheduled(fixedDelayString = "${traceops.outbox.relay-interval-ms:1000}")
    public void tick() {
        relay.relayOnce();
    }
}
