package dev.traceops.controlplane.outbox;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 只负责定时触发投递轮次；可用 traceops.outbox.relay-enabled=false 关闭。
 *
 * <p>触发走 {@link OutboxPassGate} 而不是直接调 {@code relayOnce()}：
 * 手工触发端点也能跑同一批事件，两条路径必须共享同一把单飞闸门，
 * 否则"点一下立即投递"会和正在跑的调度轮次重发同一批事件。
 */
@Component
@ConditionalOnProperty(name = "traceops.outbox.relay-enabled", havingValue = "true", matchIfMissing = true)
public class OutboxRelayScheduler {

    private final OutboxPassGate gate;

    public OutboxRelayScheduler(OutboxPassGate gate) {
        this.gate = gate;
    }

    @Scheduled(fixedDelayString = "${traceops.outbox.relay-interval-ms:1000}")
    public void tick() {
        gate.requestPass();
    }
}
