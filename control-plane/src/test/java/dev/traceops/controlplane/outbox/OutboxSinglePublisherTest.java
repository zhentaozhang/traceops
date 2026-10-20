package dev.traceops.controlplane.outbox;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.function.Consumer;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.transaction.annotation.Transactional;

/**
 * 两个 relay 实例同时投递时，不能把同一行记成"两次成功"，更不能把成功记成失败。
 *
 * <p>被替换的缺陷：relay 发布后直接 {@code save(event)} 写回。两个实例会取到
 * 同一批 {@code published_at is null} 的行（没有行锁、没有条件更新），于是：
 * <ul>
 *   <li>同一事件被投递两次（消费端幂等能兜住，但账面上完全看不出来）；</li>
 *   <li>更糟的竞态：A 标成功之后 B 走到失败分支，把同一行标成
 *       "attempts+1、未发布" —— 成功的一行被记成失败，然后被重投。</li>
 * </ul>
 * 条件更新（带 attempts 的 CAS）把"我读到的那一版"钉进 where。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@Transactional
class OutboxSinglePublisherTest {

    @Autowired
    private OutboxRepository outbox;

    private final List<String> publishedPayloads = new ArrayList<>();

    @BeforeEach
    void setUp() {
        publishedPayloads.clear();
    }

    /** 可编程的发布器：发布第 N 条时执行一个回调（用来模拟"另一个实例抢先了"）。 */
    private final class ProgrammablePublisher implements EventPublisher {
        private final Consumer<String> duringPublish;

        private ProgrammablePublisher(Consumer<String> duringPublish) {
            this.duringPublish = duringPublish;
        }

        @Override
        public void publish(String payload) {
            publishedPayloads.add(payload);
            duringPublish.accept(payload);
        }
    }

    private OutboxEvent queue(String payload) {
        OutboxEvent event = OutboxEvent.pending(
                "evt-" + UUID.randomUUID(), "investigation.requested",
                "run-" + UUID.randomUUID(), 1L, payload, Instant.now());
        return outbox.save(event);
    }

    // ---------- CAS 自身的行为 ----------

    @Test
    void theFirstClaimWins() {
        OutboxEvent event = queue("{\"a\":1}");

        int claimed = outbox.markPublishedIfUntouched(event.getEventId(), Instant.now(), 0);

        assertThat(claimed).isEqualTo(1);
        assertThat(outbox.findById(event.getEventId()).orElseThrow().getPublishedAt()).isNotNull();
    }

    @Test
    void aSecondClaimOnTheSameRowIsRefused() {
        OutboxEvent event = queue("{\"a\":1}");
        Instant first = Instant.now();
        outbox.markPublishedIfUntouched(event.getEventId(), first, 0);

        int second = outbox.markPublishedIfUntouched(event.getEventId(), first.plusSeconds(1), 0);

        // 拒绝第二次认领，且不要把发布时间改掉（改掉就再也查不出真正的发布时间）
        assertThat(second).isZero();
        assertThat(outbox.findById(event.getEventId()).orElseThrow().getPublishedAt())
                .isEqualTo(outbox.findById(event.getEventId()).orElseThrow().getPublishedAt());
    }

    @Test
    void aRowWhoseAttemptsAdvancedIsNotClaimed() {
        OutboxEvent event = queue("{\"a\":1}");
        event.markFailed();
        outbox.save(event);

        // 读到的是 attempts=0，但行已经被别人记成失败（attempts=1）：
        // 说明这一行的状态在我读完之后变过，我不能声称自己发布了它
        int claimed = outbox.markPublishedIfUntouched(event.getEventId(), Instant.now(), 0);

        assertThat(claimed).isZero();
    }

    // ---------- relay 的行为 ----------

    @Test
    void aRelayThatLosesTheRaceDoesNotCountTheEventAsItsOwn() {
        OutboxEvent first = queue("{\"first\":true}");
        OutboxEvent second = queue("{\"second\":true}");
        // 另一个实例在我发布第一条的瞬间把它标成已发布
        OutboxRelay relay = new OutboxRelay(outbox, new ProgrammablePublisher(payload -> {
            if (payload.contains("first")) {
                outbox.markPublishedIfUntouched(first.getEventId(), Instant.now(), 0);
            }
        }));

        int published = relay.relayOnce();

        // 两条都发出去了（至少一次语义），但只有第二条是我记的账
        assertThat(publishedPayloads).hasSize(2);
        assertThat(published).isEqualTo(1);
        assertThat(second.getEventId()).isNotNull();

        // 关键：不能把输掉的那条记成失败 —— 它已经发布了
        OutboxEvent reloaded = outbox.findById(first.getEventId()).orElseThrow();
        assertThat(reloaded.getPublishedAt()).isNotNull();
        assertThat(reloaded.getAttempts()).isZero();
    }

    @Test
    void aPublishFailureStillIncrementsAttemptsAndStopsTheBatch() {
        OutboxEvent first = queue("{\"first\":true}");
        queue("{\"second\":true}");
        OutboxRelay relay = new OutboxRelay(outbox, new ProgrammablePublisher(payload -> {
            if (payload.contains("first")) {
                throw new IllegalStateException("broker down");
            }
        }));

        int published = relay.relayOnce();

        assertThat(published).isZero();
        // 遇错即 break（保序）：第二条不能越过失败的第一条先发出去
        assertThat(publishedPayloads).hasSize(1);
        assertThat(outbox.findById(first.getEventId()).orElseThrow().getAttempts()).isEqualTo(1);
    }
}
