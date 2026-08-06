package dev.traceops.controlplane.correlation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import dev.traceops.controlplane.correlation.AlertCorrelation.Candidate;
import dev.traceops.controlplane.correlation.AlertCorrelation.Reason;
import dev.traceops.controlplane.correlation.AlertCorrelation.Verdict;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;

class AlertCorrelationTest {

    private static final Instant T0 = Instant.parse("2026-10-30T10:02:00Z");

    private final AlertCorrelation correlation = new AlertCorrelation(
            Duration.ofMinutes(10),
            AlertCorrelation.staticTopology(Set.of(List.of("order-service", "redis"))));

    private Candidate alert(String fingerprint, String service, int offsetSeconds) {
        return new Candidate(fingerprint, "prod", service, T0.plusSeconds(offsetSeconds));
    }

    @Test
    void sameFingerprintAlwaysMerges() {
        Verdict verdict = correlation.shouldCorrelate(alert("fp-1", "order-service", 0),
                alert("fp-1", "order-service", 60));
        assertThat(verdict.correlated()).isTrue();
        assertThat(verdict.reason()).isEqualTo(Reason.SAME_FINGERPRINT);
    }

    @Test
    void sameServiceWithinWindowMerges() {
        Verdict verdict = correlation.shouldCorrelate(alert("fp-a", "order-service", 0),
                alert("fp-b", "order-service", 30));
        assertThat(verdict.correlated()).isTrue();
        assertThat(verdict.reason()).isEqualTo(Reason.SAME_SERVICE_WITHIN_WINDOW);
    }

    @Test
    void sameServiceOutsideWindowDoesNotMerge() {
        Verdict verdict = correlation.shouldCorrelate(alert("fp-a", "order-service", 0),
                alert("fp-b", "order-service", 1200));
        assertThat(verdict.correlated()).isFalse();
        assertThat(verdict.reason()).isEqualTo(Reason.UNRELATED);
    }

    @Test
    void dependentServiceWithinWindowMerges() {
        // order-service 的 500 上升 + redis 超时，属于同一次故障
        Verdict verdict = correlation.shouldCorrelate(alert("fp-500", "order-service", 0),
                alert("fp-redis", "redis", 25));
        assertThat(verdict.correlated()).isTrue();
        assertThat(verdict.reason()).isEqualTo(Reason.DEPENDENT_SERVICE_WITHIN_WINDOW);
    }

    @Test
    void unrelatedServicesDoNotMerge() {
        Verdict verdict = correlation.shouldCorrelate(alert("fp-a", "order-service", 0),
                alert("fp-b", "billing-api", 10));
        assertThat(verdict.correlated()).isFalse();
    }

    @Test
    void differentEnvironmentNeverMerges() {
        Candidate prod = new Candidate("fp-a", "prod", "order-service", T0);
        Candidate staging = new Candidate("fp-b", "staging", "order-service", T0);
        Verdict verdict = correlation.shouldCorrelate(prod, staging);
        assertThat(verdict.correlated()).isFalse();
        assertThat(verdict.reason()).isEqualTo(Reason.UNRELATED);
    }

    @Test
    void groupingMergesCascadeIntoOneIncident() {
        // 蓝图 §2.1 的例子：三条告警应形成一次事故
        List<Candidate> alerts = List.of(
                alert("fp-http500", "order-service", 0),
                alert("fp-redis", "redis", 20),
                alert("fp-latency", "order-service", 45));

        List<List<Candidate>> groups = correlation.group(alerts);

        assertThat(groups).hasSize(1);
        assertThat(groups.getFirst()).hasSize(3);
    }

    @Test
    void groupingKeepsUnrelatedAlertSeparate() {
        List<Candidate> alerts = List.of(
                alert("fp-http500", "order-service", 0),
                alert("fp-payment", "payment-service", 900));

        List<List<Candidate>> groups = correlation.group(alerts);

        assertThat(groups).hasSize(2);
    }

    @Test
    void groupingIsDeterministic() {
        List<Candidate> alerts = List.of(
                alert("fp-a", "order-service", 0),
                alert("fp-b", "redis", 20),
                alert("fp-c", "billing-api", 300));

        List<List<Candidate>> first = correlation.group(alerts);
        List<List<Candidate>> second = correlation.group(alerts);

        assertThat(first).isEqualTo(second);
    }

    @Test
    void emptyBatchYieldsNoGroups() {
        assertThat(correlation.group(List.of())).isEmpty();
    }

    @Test
    void candidateRequiresFingerprintAndService() {
        assertThatThrownBy(() -> new Candidate("", "prod", "order-service", T0))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("fingerprint");
        assertThatThrownBy(() -> new Candidate("fp", "prod", "", T0))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("service");
    }

    @Test
    void zeroWindowRejected() {
        assertThatThrownBy(() -> new AlertCorrelation(Duration.ZERO,
                AlertCorrelation.staticTopology(Set.of())))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
