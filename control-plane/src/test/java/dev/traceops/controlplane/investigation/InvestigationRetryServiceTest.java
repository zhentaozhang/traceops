package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.domain.IncidentStatus;
import dev.traceops.controlplane.outbox.OutboxRepository;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.transaction.annotation.Transactional;

/**
 * 重试闭环的真实行为测试。
 *
 * <p>此前 {@code InvestigationResultService} 遇到可重试失败时返回 {@code RETRY_PENDING}，
 * 理由写着"等新一轮 Run"——但**没有任何东西会发出那一轮**。
 * 事故永远停在 INVESTIGATING，失败一次就再也不动了。
 *
 * <p>本测试覆盖闭环的四段：
 * <ol>
 *   <li>可重试失败 → 真的产生一条新的 {@code investigation.requested}（新的 run_id）</li>
 *   <li>新的那条请求**指向同一个事故**（否则重试会变成孤儿任务）</li>
 *   <li>尝试次数有上限：超过上限 → 事故转 FAILED（不能无限重试烧预算）</li>
 *   <li>不可重试失败 → 不产生任何新请求</li>
 * </ol>
 *
 * <p>不 mock 被测核心：真实 H2、真实状态机、真实 outbox。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@Transactional
class InvestigationRetryServiceTest {

    @Autowired
    private InvestigationResultService resultService;

    @Autowired
    private IncidentRepository incidents;

    @Autowired
    private OutboxRepository outbox;

    @Autowired
    private ObjectMapper mapper;

    private String incidentId;

    @BeforeEach
    void setUp() {
        incidentId = "inc-" + UUID.randomUUID();
        incidents.save(Incident.open(incidentId, "fp-" + incidentId, "order-service",
                "redis pool exhausted", "critical", Instant.now()));
        Incident incident = incidents.findById(incidentId).orElseThrow();
        incident.transitionTo(IncidentStatus.INVESTIGATING, Instant.now());
        incidents.save(incident);
    }

    // ---------- 消息构造 ----------

    private ObjectNode failedPayload(String runId, String reason, boolean retryable) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("investigation_run_id", runId);
        payload.put("terminal_status", "FAILED");
        payload.put("failure_reason", reason);
        payload.put("retryable", retryable);
        payload.put("failed_node", "collect");
        payload.put("checkpoint_id", "ckpt-" + runId);
        return payload;
    }

    private String failedMessage(String eventId, String runId, String reason, boolean retryable) {
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("event_id", eventId);
        envelope.put("event_type", "investigation.failed");
        envelope.put("schema_version", "1.0");
        envelope.put("occurred_at", Instant.now().toString());
        envelope.put("correlation_id", incidentId);
        envelope.putNull("causation_id");
        envelope.put("aggregate_id", runId);
        envelope.put("aggregate_version", 1);
        envelope.put("producer", "agent-runtime");
        return "{\"envelope\":" + envelope + ",\"payload\":" + failedPayload(runId, reason, retryable) + "}";
    }

    private static String evt() {
        return "evt-" + UUID.randomUUID();
    }

    private static String run() {
        return "run-" + UUID.randomUUID();
    }

    private IncidentStatus currentStatus() {
        return incidents.findById(incidentId).orElseThrow().getStatus();
    }

    /** outbox 里由重试产生的 investigation.requested 消息体。 */
    private List<Map<String, Object>> retryRequests() {
        List<Map<String, Object>> requests = new java.util.ArrayList<>();
        for (dev.traceops.controlplane.outbox.OutboxEvent event : outbox.findAll()) {
            if (!"investigation.requested".equals(event.getEventType())) {
                continue;
            }
            try {
                requests.add(mapper.readValue(event.getPayload(), Map.class));
            } catch (Exception ex) {
                throw new IllegalStateException(ex);
            }
        }
        return requests;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> payloadOf(Map<String, Object> message) {
        return (Map<String, Object>) message.get("payload");
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> envelopeOf(Map<String, Object> message) {
        return (Map<String, Object>) message.get("envelope");
    }

    // ---------- 1. 重试请求真的被发出 ----------

    @Test
    void retryableFailureProducesANewInvestigationRequest() {
        String firstRun = run();

        Applied applied = resultService.apply(
                failedMessage(evt(), firstRun, "MCP_SERVER_UNAVAILABLE", true));

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.RETRY_PENDING);
        // 之前这里恒为空：RETRY_PENDING 只是记账，没有任何新请求被发出去
        assertThat(retryRequests()).hasSize(1);
    }

    @Test
    void theRetryRequestUsesANewRunId() {
        String firstRun = run();

        resultService.apply(failedMessage(evt(), firstRun, "MCP_SERVER_UNAVAILABLE", true));

        Map<String, Object> payload = payloadOf(retryRequests().getFirst());
        // 重试必须是**新的一轮 Run**：同一 run_id 会被幂等账本当成同一轮结算掉
        assertThat(payload.get("investigation_run_id")).isNotEqualTo(firstRun);
        assertThat((String) payload.get("investigation_run_id")).startsWith("run-");
    }

    @Test
    void theRetryRequestPointsAtTheSameIncident() {
        resultService.apply(failedMessage(evt(), run(), "MCP_SERVER_UNAVAILABLE", true));

        Map<String, Object> payload = payloadOf(retryRequests().getFirst());
        // 指向别的事故 = 重试变成孤儿任务：那个事故的状态永远不动
        assertThat(payload.get("incident_id")).isEqualTo(incidentId);
    }

    @Test
    void theRetryRequestCarriesTheSameServiceHint() {
        resultService.apply(failedMessage(evt(), run(), "MCP_SERVER_UNAVAILABLE", true));

        Map<String, Object> payload = payloadOf(retryRequests().getFirst());
        // service_hint 是 Agent 的调查起点，丢了它 Agent 无从下手
        assertThat(payload.get("service_hint")).isEqualTo("order-service");
    }

    @Test
    void theRetryRequestLinksBackToTheFailedEvent() {
        String eventId = evt();

        resultService.apply(failedMessage(eventId, run(), "MCP_SERVER_UNAVAILABLE", true));

        Map<String, Object> envelope = envelopeOf(retryRequests().getFirst());
        // causation_id 必须指向导致这次重试的失败事件，否则因果链断掉，
        // 事后无法回答"这轮重试是因为哪次失败"
        assertThat(envelope.get("causation_id")).isEqualTo(eventId);
    }

    // ---------- 2. 不可重试失败不产生新请求 ----------

    @Test
    void nonRetryableFailureProducesNoNewRequest() {
        Applied applied = resultService.apply(
                failedMessage(evt(), run(), "DATASOURCE_MISSING", false));

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.APPLIED);
        assertThat(retryRequests()).isEmpty();
        assertThat(currentStatus()).isEqualTo(IncidentStatus.FAILED);
    }

    // ---------- 3. 尝试次数有上限 ----------

    @Test
    void attemptsAreBoundedAndTheIncidentEndsUpFailed() {
        // 连续的可重试失败，每次都应该再发一轮请求，直到用完预算
        int emitted = 0;
        for (int i = 0; i < 10; i++) {
            resultService.apply(failedMessage(evt(), run(), "MCP_SERVER_UNAVAILABLE", true));
            emitted = retryRequests().size();
            if (currentStatus() == IncidentStatus.FAILED) {
                break;
            }
        }

        // 关键断言：重试不是无限的。超过上限后事故必须落到 FAILED，
        // 否则一次 MCP 抖动就会把预算烧光。
        assertThat(currentStatus()).isEqualTo(IncidentStatus.FAILED);
        assertThat(emitted).isGreaterThan(0);
    }

    @Test
    void theRetryBudgetIsNotExhaustedByASingleFailure() {
        resultService.apply(failedMessage(evt(), run(), "MCP_SERVER_UNAVAILABLE", true));

        // 一次失败不该直接判死：至少还要留出重试的余地
        assertThat(currentStatus()).isNotEqualTo(IncidentStatus.FAILED);
    }

    // ---------- 4. 重复事件不会重复触发重试 ----------

    @Test
    void aDuplicateFailureEventDoesNotProduceASecondRetryRequest() {
        String eventId = evt();
        String runId = run();

        resultService.apply(failedMessage(eventId, runId, "MCP_SERVER_UNAVAILABLE", true));
        assertThat(retryRequests()).hasSize(1);

        resultService.apply(failedMessage(eventId, runId, "MCP_SERVER_UNAVAILABLE", true));

        // 幂等：同一条失败事件重发时不能再发一轮调查，否则一次网络抖动
        // 会把同一事故重试 N 次
        assertThat(retryRequests()).hasSize(1);
    }

    // ---------- 5. 重试请求本身必须符合契约 ----------

    @Test
    void theRetryRequestMatchesTheVersionedContract() throws Exception {
        resultService.apply(failedMessage(evt(), run(), "MCP_SERVER_UNAVAILABLE", true));

        String payload = mapper.writeValueAsString(retryRequests().getFirst());
        assertThat(validateAgainstSchema("events/v1/investigation-requested.schema.json", payload))
                .as("contract violations: %s", payload)
                .isEmpty();
    }

    private java.util.Set<com.networknt.schema.ValidationMessage> validateAgainstSchema(
            String relativePath, String json) throws Exception {
        java.nio.file.Path contracts = java.nio.file.Path.of("..", "contracts");
        JsonNode schemaNode = mapper.readTree(
                java.nio.file.Files.readString(contracts.resolve(relativePath)));
        JsonNode envelopeNode = mapper.readTree(java.nio.file.Files.readString(
                contracts.resolve("common/event-envelope.schema.json")));
        JsonNode timeRangeNode = mapper.readTree(java.nio.file.Files.readString(
                contracts.resolve("common/time-range.schema.json")));

        var root = (ObjectNode) schemaNode;
        var defs = root.putObject("$defs");
        defs.set("event-envelope", stripIds(envelopeNode));
        defs.set("time-range", stripIds(timeRangeNode));
        inlineRef(root, "../../common/event-envelope.schema.json", "#/$defs/event-envelope");
        inlineRef(root, "../../common/time-range.schema.json", "#/$defs/time-range");

        com.networknt.schema.JsonSchemaFactory factory =
                com.networknt.schema.JsonSchemaFactory.getInstance(
                        com.networknt.schema.SpecVersion.VersionFlag.V202012);
        return factory.getSchema(root).validate(mapper.readTree(json));
    }

    private JsonNode stripIds(JsonNode node) {
        var copy = node.deepCopy();
        if (copy instanceof ObjectNode obj) {
            obj.remove("$id");
            obj.remove("$schema");
        }
        return copy;
    }

    private void inlineRef(JsonNode node, String oldRef, String newRef) {
        if (node.isObject()) {
            var obj = (ObjectNode) node;
            if (oldRef.equals(obj.path("$ref").asText())) {
                obj.put("$ref", newRef);
            }
            obj.elements().forEachRemaining(child -> inlineRef(child, oldRef, newRef));
        } else if (node.isArray()) {
            node.elements().forEachRemaining(child -> inlineRef(child, oldRef, newRef));
        }
    }
}
