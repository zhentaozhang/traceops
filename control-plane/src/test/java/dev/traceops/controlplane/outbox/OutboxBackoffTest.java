package dev.traceops.controlplane.outbox;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.transaction.annotation.Transactional;

/**
 * 退避（backoff）必须真的**推迟投递**，而不只是把一个时间戳写进数据库。
 *
 * <p>为什么单独测：如果 relay 仍然按"created_at 排序取全部待投递"来工作，
 * 那么 not_before 只是个装饰性的字段 —— 重试会立刻发出去，
 * 等于把"失败后等 5 秒再试"变成了"失败后立刻再试"，
 * 一次下游抖动会被放大成风暴，而代码看上去一切正常。
 *
 * <p>还有一条容易漏的：退避中的行不能**堵住**后面的行。
 * relay 遇错即 break（保序），一条还没到时间的重试卡在批次中间，
 * 就会把后面所有事件一起卡住。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@Transactional
class OutboxBackoffTest {

    @Autowired
    private OutboxRepository repository;

    @Autowired
    private OutboxRelay relay;

    /**
     * 用真实时钟做基准：relay 内部用的是 {@code Instant.now()}，
     * 拿一个写死的过去时刻当基准会让"还没到时间"的事件其实早已到期。
     */
    private final Instant now = Instant.now();

    private OutboxEvent after(String id, Instant notBefore) {
        return OutboxEvent.pendingAfter(id, "investigation.requested", "run-" + id, 1L,
                "{\"envelope\":{}}", now, notBefore);
    }

    @Test
    void anEventThatIsNotDueYetIsNotDelivered() {
        repository.save(after("evt-later", now.plusSeconds(300)));

        int published = relay.relayOnce();

        assertThat(published).isZero();
        assertThat(repository.findById("evt-later").orElseThrow().isPublished()).isFalse();
    }

    @Test
    void anEventIsDeliveredOnceItsBackoffHasElapsed() {
        repository.save(after("evt-due", now.minusSeconds(1)));

        int published = relay.relayOnce();

        assertThat(published).isEqualTo(1);
        assertThat(repository.findById("evt-due").orElseThrow().isPublished()).isTrue();
    }

    @Test
    void aDeferredEventDoesNotBlockTheEventsBehindIt() {
        // 顺序很重要：先建"还没到时间"的，再建"可以发"的。
        // 如果 relay 是取全部待投递再逐条判断，退避行会挡住后面那条。
        repository.save(after("evt-blocked", now.plusSeconds(300)));
        repository.save(after("evt-ready", now.minusSeconds(1)));

        int published = relay.relayOnce();

        assertThat(published).isEqualTo(1);
        assertThat(repository.findById("evt-ready").orElseThrow().isPublished()).isTrue();
        assertThat(repository.findById("evt-blocked").orElseThrow().isPublished()).isFalse();
    }

    @Test
    void anEventWithoutABackoffIsImmediatelyDeliverable() {
        OutboxEvent event = OutboxEvent.pending("evt-now", "investigation.requested",
                "run-now", 1L, "{\"envelope\":{}}", now);
        repository.save(event);

        assertThat(event.getNotBefore()).isNull();
        assertThat(event.isDeliverableAt(now)).isTrue();
        assertThat(relay.relayOnce()).isEqualTo(1);
    }
}
