package dev.traceops.controlplane.investigation;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 只负责定时触发 {@link InvestigationEventPoller#pollOnce()}。
 *
 * <p><b>默认关闭（{@code matchIfMissing = false}，与 outbox relay 相反）</b>：一旦开启，
 * 应用启动后就会持续连 Redis 拉消息——这对开发/测试/离线演示来说是行为改变（本地没有 Redis
 * 时会每轮打一条连接失败告警），而回程消费者在生产上又必须显式确认"我确实要接管这个流"。
 * 因此生产用 {@code traceops.investigation.poller-enabled=true} 打开，默认不打开。
 *
 * <p>{@code @Scheduled} 与 {@code @ConditionalOnProperty} 必须同处一个类，但只存在于这个
 * 薄薄的调度类里，业务逻辑 {@link InvestigationEventPoller} 是无条件注册的普通 bean：
 * 条件不满足时消失的只是"定时器"，而不是"能力"。
 */
@Component
@ConditionalOnProperty(name = "traceops.investigation.poller-enabled",
        havingValue = "true", matchIfMissing = false)
public class InvestigationEventPollerScheduler {

    private final InvestigationEventPoller poller;

    public InvestigationEventPollerScheduler(InvestigationEventPoller poller) {
        this.poller = poller;
    }

    @Scheduled(fixedDelayString = "${traceops.investigation.poller-interval-ms:1000}")
    public void tick() {
        poller.pollOnce();
    }
}
