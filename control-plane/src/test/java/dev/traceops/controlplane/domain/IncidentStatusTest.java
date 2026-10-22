package dev.traceops.controlplane.domain;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

class IncidentStatusTest {

    @ParameterizedTest
    @CsvSource({
            "NEW,INVESTIGATING,true",
            "NEW,DIAGNOSED,false",
            "NEW,RESOLVED,false",
            "INVESTIGATING,DIAGNOSED,true",
            "INVESTIGATING,RESOLVED,false",
            "DIAGNOSED,RESOLVED,true",
            "DIAGNOSED,FAILED,true",
            "DIAGNOSED,INVESTIGATING,false",
            "RESOLVED,NEW,false",
            "FAILED,NEW,false"
    })
    void transitionTable(IncidentStatus from, IncidentStatus to, boolean allowed) {
        assertThat(from.canTransitionTo(to)).isEqualTo(allowed);
    }

    @Test
    void terminalStatesHaveNoOutgoingEdges() {
        for (IncidentStatus target : IncidentStatus.values()) {
            assertThat(IncidentStatus.RESOLVED.canTransitionTo(target)).isFalse();
            assertThat(IncidentStatus.FAILED.canTransitionTo(target)).isFalse();
        }
        assertThat(IncidentStatus.RESOLVED.isTerminal()).isTrue();
        assertThat(IncidentStatus.FAILED.isTerminal()).isTrue();
        assertThat(IncidentStatus.NEW.isTerminal()).isFalse();
    }

    @Test
    void illegalTransitionIsRejectedAndStatusPreserved() {
        Instant now = Instant.parse("2026-10-31T00:00:00Z");
        Incident incident = Incident.open("inc-1", "fp-1", "order-service", "boom", "critical", now);

        incident.transitionTo(IncidentStatus.INVESTIGATING, now);
        assertThat(incident.getStatus()).isEqualTo(IncidentStatus.INVESTIGATING);

        IncidentStatus before = incident.getStatus();
        try {
            incident.transitionTo(IncidentStatus.RESOLVED, now);
            throw new AssertionError("expected IllegalTransitionException");
        } catch (IllegalTransitionException ex) {
            assertThat(ex.getFrom()).isEqualTo(before);
            assertThat(ex.getTo()).isEqualTo(IncidentStatus.RESOLVED);
            assertThat(ex.getIncidentId()).isEqualTo("inc-1");
        }
        // 失败后状态不变：状态机不能被非法转换改坏
        assertThat(incident.getStatus()).isEqualTo(before);
    }
}
