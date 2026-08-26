package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

/**
 * 回程消费者测试。
 *
 * <p>核心行为有三条，每条都能独立出错：
 * <ol>
 *   <li><b>该处理的处理了</b>——合法终态事件驱动状态机</li>
 *   <li><b>不该处理的跳过</b>——Java 自己发出的 investigation.requested 不能被自己消费</li>
 *   <li><b>ACK 判据正确</b>——NOT_FOUND 不 ACK（结论可能只是还没落库），毒消息 ACK（重投也一样坏）</li>
 * </ol>
 *
 * <p>Redis 用内存实现替代：这里要验证的是"哪些消息处理、哪些 ACK"的<b>业务判断</b>，
 * 不是 Spring Data Redis 的序列化细节。真实 Redis 的形状由
 * {@code RedisInvestigationEventSource} 负责，那是另一层的事。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "traceops.investigation.poller-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@AutoConfigureMockMvc
@Transactional
class InvestigationEventPollerTest {

    @TestConfiguration
    static class InMemorySourceConfig {
        /** 每个测试方法共享同一个实例，便于检查 acked 记录。 */
        @Bean
        @Primary
        InMemorySource inMemorySource() {
            return new InMemorySource();
        }
    }

    /** 内存事件源：记录 ACK 与否，这本身就是被断言的行为。 */
    static class InMemorySource implements InvestigationEventSource {
        private final List<StreamMessage> queued = new ArrayList<>();
        private final List<String> acked = new ArrayList<>();

        void push(String id, String payload) {
            queued.add(new StreamMessage(id, Map.of(StreamMessage.PAYLOAD_FIELD, payload)));
        }

        @Override
        public List<StreamMessage> read() {
            List<StreamMessage> batch = List.copyOf(queued);
            queued.clear();
            return batch;
        }

        @Override
        public void ack(String id) {
            acked.add(id);
        }

        boolean wasAcked(String id) {
            return acked.contains(id);
        }
    }

    @Autowired
    private MockMvc mvc;

    @Autowired
    private ObjectMapper mapper;

    @Autowired
    private InMemorySource source;

    @Autowired
    private InvestigationEventPoller poller;

    @BeforeEach
    void reset() {
        source.acked.clear();
    }

    // ---- helpers ----

    private String createIncident(String fingerprint) throws Exception {
        String body = """
                {"alerts":[{"fingerprint":"%s","status":"firing","service":"order-service",
                 "severity":"critical","summary":"pool exhausted"}]}
                """.formatted(fingerprint);
        String response = mvc.perform(post("/api/v1/alerts")
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andReturn().getResponse().getContentAsString();
        return mapper.readTree(response).path("incidentId").asText();
    }

    private void driveToInvestigating(String incidentId) throws Exception {
        mvc.perform(post("/api/v1/incidents/{id}/transitions", incidentId)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"target\":\"INVESTIGATING\"}"))
                .andExpect(status().isOk());
    }

    private String completedEvent(String incidentId, String runId, String diagnosisStatus) {
        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("event_id", "evt-" + runId);
        envelope.put("event_type", "investigation.completed");
        envelope.put("schema_version", "1.0");
        envelope.put("occurred_at", "2026-10-31T00:00:00Z");
        envelope.put("correlation_id", incidentId);
        envelope.put("causation_id", null);
        envelope.put("aggregate_id", runId);
        envelope.put("aggregate_version", 1);
        envelope.put("producer", "agent-runtime");

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("investigation_run_id", runId);
        payload.put("diagnosis_status", diagnosisStatus);
        payload.put("rca_report_ref", runId + "/report");
        payload.put("root_cause_candidates", List.of(Map.of(
                "candidate_service", "redis", "fault_type", "pool_exhaustion", "rank", 1,
                "hypothesis_status", "supported")));
        payload.put("evidence_summary", Map.of("total", 5));
        payload.put("usage", Map.of("tool_calls", 4, "tokens", 900, "rounds", 1,
                "duration_seconds", 20));
        payload.put("abstained", !"SUPPORTED".equals(diagnosisStatus));

        Map<String, Object> document = new LinkedHashMap<>();
        document.put("envelope", envelope);
        document.put("payload", payload);
        try {
            return mapper.writeValueAsString(document);
        } catch (Exception ex) {
            throw new IllegalStateException(ex);
        }
    }

    private String failedEvent(String incidentId, String runId, String reason, boolean retryable) {
        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("event_id", "evt-" + runId);
        envelope.put("event_type", "investigation.failed");
        envelope.put("schema_version", "1.0");
        envelope.put("occurred_at", "2026-10-31T00:00:00Z");
        envelope.put("correlation_id", incidentId);
        envelope.put("causation_id", null);
        envelope.put("aggregate_id", runId);
        envelope.put("aggregate_version", 1);
        envelope.put("producer", "agent-runtime");

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("investigation_run_id", runId);
        payload.put("terminal_status", "FAILED");
        payload.put("failure_reason", reason);
        payload.put("retryable", retryable);
        payload.put("failed_node", "collect");
        payload.put("usage_at_failure", Map.of("tool_calls", 2, "tokens", 100, "rounds", 1));

        Map<String, Object> document = new LinkedHashMap<>();
        document.put("envelope", envelope);
        document.put("payload", payload);
        try {
            return mapper.writeValueAsString(document);
        } catch (Exception ex) {
            throw new IllegalStateException(ex);
        }
    }

    private String requestedEvent(String incidentId, String runId) {
        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("event_id", "evt-" + runId);
        envelope.put("event_type", "investigation.requested");
        envelope.put("schema_version", "1.0");
        envelope.put("occurred_at", "2026-10-31T00:00:00Z");
        envelope.put("correlation_id", incidentId);
        envelope.put("producer", "control-plane");

        Map<String, Object> document = new LinkedHashMap<>();
        document.put("envelope", envelope);
        document.put("payload", Map.of("investigation_run_id", runId));
        try {
            return mapper.writeValueAsString(document);
        } catch (Exception ex) {
            throw new IllegalStateException(ex);
        }
    }

    // ---- tests ----

    @Test
    void supportedResultDrivesStateMachineAndAcks() throws Exception {
        String incidentId = createIncident("fp-poll-1");
        driveToInvestigating(incidentId);
        source.push("1-0", completedEvent(incidentId, "run-p1", "SUPPORTED"));

        PollResult result = poller.pollOnce();

        assertThat(result.applied()).isEqualTo(1);
        assertThat(result.processed()).isEqualTo(1);
        assertThat(source.wasAcked("1-0")).isTrue();
    }

    @Test
    void requestedEventFromControlPlaneIsSkippedAndAcked() throws Exception {
        String incidentId = createIncident("fp-poll-2");
        source.push("2-0", requestedEvent(incidentId, "run-p2"));

        PollResult result = poller.pollOnce();

        assertThat(result.skipped()).isEqualTo(1);
        assertThat(result.applied()).isZero();
        // 自己发出的请求不能被自己消费；留在 PEL 只会堵塞
        assertThat(source.wasAcked("2-0")).isTrue();
    }

    @Test
    void inconclusiveResultCountsAsNeedsReviewAndAcks() throws Exception {
        String incidentId = createIncident("fp-poll-3");
        driveToInvestigating(incidentId);
        source.push("3-0", completedEvent(incidentId, "run-p3", "INCONCLUSIVE"));

        PollResult result = poller.pollOnce();

        assertThat(result.needsReview()).isEqualTo(1);
        assertThat(source.wasAcked("3-0")).isTrue();
    }

    @Test
    void unknownIncidentIsNotAckedSoItCanBeRetried() throws Exception {
        // 事故不存在：可能是回程消息先到、事故落库后到（跨服务乱序）
        source.push("4-0", completedEvent("inc-does-not-exist", "run-p4", "SUPPORTED"));

        PollResult result = poller.pollOnce();

        assertThat(result.notFound()).isEqualTo(1);
        // 关键：不 ACK。ACK 会把这条结论永久丢掉，事故再也不会被推进
        assertThat(source.wasAcked("4-0")).isFalse();
    }

    @Test
    void duplicateEventIsSkippedAndAcked() throws Exception {
        String incidentId = createIncident("fp-poll-5");
        driveToInvestigating(incidentId);
        String event = completedEvent(incidentId, "run-p5", "SUPPORTED");
        source.push("5-0", event);
        source.push("5-1", event);  // 同一 event_id 重复投递

        PollResult result = poller.pollOnce();

        assertThat(result.applied()).isEqualTo(1);
        assertThat(result.skipped()).isEqualTo(1);
        assertThat(source.wasAcked("5-0")).isTrue();
        assertThat(source.wasAcked("5-1")).isTrue();
    }

    @Test
    void badJsonIsAckedAndCountedAsError() throws Exception {
        source.push("6-0", "{not valid json");

        PollResult result = poller.pollOnce();

        assertThat(result.errors()).isEqualTo(1);
        // 毒消息留在 PEL 只会永久堵塞消费者，重投一万次也一样坏
        assertThat(source.wasAcked("6-0")).isTrue();
    }

    @Test
    void contractViolationIsAckedAndCountedAsError() throws Exception {
        // producer 不是 agent-runtime：身份校验必须拒绝
        String event = completedEvent("inc-any", "run-p7", "SUPPORTED")
                .replace("\"producer\":\"agent-runtime\"", "\"producer\":\"fault-controller\"");
        source.push("7-0", event);

        PollResult result = poller.pollOnce();

        assertThat(result.errors()).isEqualTo(1);
        assertThat(source.wasAcked("7-0")).isTrue();
    }

    @Test
    void retryableFailureKeepsIncidentInvestigatingAndAcks() throws Exception {
        String incidentId = createIncident("fp-poll-8");
        driveToInvestigating(incidentId);
        source.push("8-0", failedEvent(incidentId, "run-p8", "MCP_SERVER_UNAVAILABLE", true));

        PollResult result = poller.pollOnce();

        assertThat(result.retryPending()).isEqualTo(1);
        assertThat(source.wasAcked("8-0")).isTrue();
    }

    @Test
    void emptyBatchYieldsZeroCounters() {
        PollResult result = poller.pollOnce();
        assertThat(result.processed()).isZero();
        assertThat(result.isClean()).isTrue();
        assertThat(source.acked).isEmpty();
    }

    @Test
    void multipleMessagesInOneBatchAreEachHandled() throws Exception {
        String okIncident = createIncident("fp-poll-9");
        driveToInvestigating(okIncident);
        source.push("9-0", completedEvent(okIncident, "run-p9a", "SUPPORTED"));
        source.push("9-1", requestedEvent(okIncident, "run-p9b"));
        source.push("9-2", completedEvent("inc-missing", "run-p9c", "SUPPORTED"));

        PollResult result = poller.pollOnce();

        assertThat(result.processed()).isEqualTo(3);
        assertThat(result.applied()).isEqualTo(1);
        assertThat(result.skipped()).isEqualTo(1);
        assertThat(result.notFound()).isEqualTo(1);
        assertThat(source.acked).containsExactly("9-0", "9-1");
    }
}
