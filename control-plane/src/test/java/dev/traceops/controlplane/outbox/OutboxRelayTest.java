package dev.traceops.controlplane.outbox;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@Transactional
class OutboxRelayTest {

    @Autowired
    private OutboxRepository repository;

    @Autowired
    private OutboxRelay relay;

    private OutboxEvent pending(String id) {
        return OutboxEvent.pending(id, "investigation.requested", "run-1", 1L, "{\"envelope\":{}}",
                Instant.parse("2026-10-31T00:00:00Z"));
    }

    @Test
    void successfulPublishMarksEventPublished() {
        repository.save(pending("evt-a"));

        int published = relay.relayOnce();

        assertThat(published).isEqualTo(1);
        assertThat(repository.countByPublishedAtIsNull()).isZero();
        assertThat(repository.findById("evt-a").orElseThrow().isPublished()).isTrue();
    }

    @Test
    void alreadyPublishedEventsAreNotRepublished() {
        OutboxEvent event = pending("evt-b");
        event.markPublished(Instant.parse("2026-10-31T00:01:00Z"));
        repository.save(event);

        assertThat(relay.relayOnce()).isZero();
        assertThat(repository.countByPublishedAtIsNull()).isZero();
    }
}
