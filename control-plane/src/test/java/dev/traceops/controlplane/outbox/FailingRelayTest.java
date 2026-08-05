package dev.traceops.controlplane.outbox;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import org.springframework.transaction.annotation.Transactional;

/** 投递失败必须留下可重试的记录（attempts+1、published_at 为空）。 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@Transactional
class FailingRelayTest {

    @TestConfiguration
    static class FailingPublisherConfig {
        @Bean
        @Primary
        EventPublisher failingPublisher() {
            return message -> {
                throw new IllegalStateException("redis unavailable");
            };
        }
    }

    @Autowired
    private OutboxRepository repository;

    @Autowired
    private OutboxRelay relay;

    @Test
    void failedPublishKeepsEventPendingAndCountsAttempt() {
        repository.save(OutboxEvent.pending("evt-fail", "investigation.requested", "run-2", 1L,
                "{\"envelope\":{}}", Instant.parse("2026-10-31T00:00:00Z")));

        int published = relay.relayOnce();

        assertThat(published).isZero();
        assertThat(repository.countByPublishedAtIsNull()).isEqualTo(1);
        OutboxEvent event = repository.findById("evt-fail").orElseThrow();
        assertThat(event.getAttempts()).isEqualTo(1);
        assertThat(event.isPublished()).isFalse();
    }
}
