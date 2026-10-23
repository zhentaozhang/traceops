package dev.traceops.controlplane.notify;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/**
 * 通知是"结论能不能到达人"的唯一路径。它的失败模式都很安静：
 * 不发、发晚了、发给了一个没人看的地方。所以这些用例断言的是
 * "什么时候必须发、什么时候必须不发"。
 */
class IncidentNotifierTest {

    private static final Instant AT = Instant.parse("2026-03-12T14:32:00Z");

    /** 记录收到的通知，可编程失败。 */
    private static final class RecordingSender implements NotificationSender {
        final List<IncidentNotification> sent = new CopyOnWriteArrayList<>();
        RuntimeException failure;

        @Override
        public void send(IncidentNotification notification) {
            if (failure != null) {
                throw failure;
            }
            sent.add(notification);
        }
    }

    private static IncidentAppliedEvent event(String outcome, Instant at) {
        return new IncidentAppliedEvent("inc-1", "payment-service", "high", "ALERT",
                "DIAGNOSED", outcome, "diagnosis_status=SUPPORTED", "spool://report-1", at);
    }

    @Test
    void aSupportedDiagnosisIsDelivered() {
        RecordingSender sender = new RecordingSender();
        IncidentNotifier notifier = new IncidentNotifier(List.of(sender));

        assertThat(notifier.deliver(event("APPLIED", AT), AT)).isTrue();
        assertThat(sender.sent).hasSize(1);
        assertThat(sender.sent.get(0).subject()).isEqualTo("[DIAGNOSED] payment-service (inc-1)");
        assertThat(sender.sent.get(0).body())
                .contains("incident: inc-1")
                .contains("status: DIAGNOSED")
                .contains("report: spool://report-1")
                .contains("why it stopped here: diagnosis_status=SUPPORTED");
    }

    @Test
    void needsHumanReviewIsAlsoDeliveredBecauseItIsTheOneNobodyCanIgnore() {
        RecordingSender sender = new RecordingSender();
        IncidentNotifier notifier = new IncidentNotifier(List.of(sender));

        assertThat(notifier.deliver(event("NEEDS_HUMAN_REVIEW", AT), AT)).isTrue();
        assertThat(sender.sent).hasSize(1);
    }

    @Test
    void routineOutcomesDoNotInterruptAnyone() {
        RecordingSender sender = new RecordingSender();
        IncidentNotifier notifier = new IncidentNotifier(List.of(sender));

        for (String outcome : List.of("REJECTED", "DUPLICATE", "NOT_FOUND", "RETRY_SCHEDULED")) {
            assertThat(notifier.deliver(event(outcome, AT), AT)).isFalse();
        }
        assertThat(sender.sent).isEmpty();
    }

    @Test
    void aStaleNotificationIsDroppedRatherThanDescribingAnOldState() {
        RecordingSender sender = new RecordingSender();
        IncidentNotifier notifier = new IncidentNotifier(List.of(sender));

        Instant muchLater = AT.plus(IncidentAppliedEvent.DELIVERY_BUDGET).plusSeconds(1);
        assertThat(notifier.deliver(event("APPLIED", AT), muchLater)).isFalse();
        assertThat(sender.sent).isEmpty();
    }

    @Test
    void aFailingSenderIsContainedAndTheOtherSenderStillGetsIt() {
        RecordingSender broken = new RecordingSender();
        broken.failure = new IllegalStateException("webhook is down");
        RecordingSender healthy = new RecordingSender();
        IncidentNotifier notifier = new IncidentNotifier(List.of(broken, healthy));

        assertThat(notifier.deliver(event("APPLIED", AT), AT)).isTrue();
        assertThat(healthy.sent).hasSize(1);
    }

    @Test
    void noConfiguredSenderMeansNothingIsSentAndNothingBreaks() {
        IncidentNotifier notifier = new IncidentNotifier(List.of());
        assertThat(notifier.deliver(event("APPLIED", AT), AT)).isFalse();
    }

    @Test
    void aMissingReportReferenceDoesNotAppearAsNull() {
        IncidentNotification notification = new IncidentNotification("inc-2", "api", "low", "MANUAL",
                "FAILED", "APPLIED", "", null, AT);
        assertThat(notification.body()).doesNotContain("report:");
        assertThat(notification.subject()).isEqualTo("[FAILED] api (inc-2)");
    }

    /** 真的发一次 HTTP，确认请求体是接收方能用的形状。 */
    @org.junit.jupiter.api.Nested
    class WebhookTest {
        private HttpServer server;
        private final List<String> bodies = new ArrayList<>();

        @BeforeEach
        void startServer() throws Exception {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext("/hook", exchange -> {
                bodies.add(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
                exchange.sendResponseHeaders(204, -1);
                exchange.close();
            });
            server.start();
        }

        @AfterEach
        void stopServer() {
            server.stop(0);
        }

        @Test
        void theWebhookPayloadCarriesBothFieldsAndText() throws Exception {
            String url = "http://127.0.0.1:" + server.getAddress().getPort() + "/hook";
            WebhookNotificationSender sender = new WebhookNotificationSender(
                    new ObjectMapper(), url, 2000);

            sender.send(new IncidentNotification("inc-3", "checkout-service", "critical", "ALERT",
                    "DIAGNOSED", "APPLIED", "pool exhausted", "spool://r-9", AT));

            assertThat(bodies).hasSize(1);
            @SuppressWarnings("unchecked")
            Map<String, Object> payload = new ObjectMapper().readValue(bodies.get(0), Map.class);
            assertThat(payload)
                    .containsEntry("incident_id", "inc-3")
                    .containsEntry("service", "checkout-service")
                    .containsEntry("outcome", "APPLIED")
                    .containsEntry("report_ref", "spool://r-9");
            assertThat(String.valueOf(payload.get("text"))).contains("status: DIAGNOSED");
        }

        @Test
        void anUnreachableWebhookDoesNotThrow() {
            WebhookNotificationSender sender = new WebhookNotificationSender(
                    new ObjectMapper(), "http://127.0.0.1:1/hook", 300);
            // 端口 1 上没有监听者：连接失败必须被吞掉，因为通知失败不是业务失败。
            sender.send(new IncidentNotification("inc-4", "api", "low", "MANUAL",
                    "FAILED", "APPLIED", null, null, AT));
        }
    }
}
