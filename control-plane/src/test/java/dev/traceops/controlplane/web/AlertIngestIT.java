package dev.traceops.controlplane.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SpecVersion;
import dev.traceops.controlplane.outbox.OutboxEvent;
import dev.traceops.controlplane.outbox.OutboxRepository;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@AutoConfigureMockMvc
@Transactional
class AlertIngestIT {

    private static final Path CONTRACTS = Path.of("..", "contracts");

    @Autowired
    private MockMvc mvc;

    @Autowired
    private ObjectMapper mapper;

    @Autowired
    private OutboxRepository outbox;

    private String alertBody(String fingerprint, String status, String service, String severity, String summary) {
        Map<String, Object> alert = new LinkedHashMap<>();
        alert.put("fingerprint", fingerprint);
        alert.put("status", status);
        alert.put("service", service);
        alert.put("severity", severity);
        alert.put("summary", summary);
        return """
                {"alerts":[%s]}
                """.formatted(toJson(alert));
    }

    private String toJson(Map<String, Object> value) {
        try {
            return mapper.writeValueAsString(value);
        } catch (Exception ex) {
            throw new IllegalStateException(ex);
        }
    }

    @Test
    void firingAlertCreatesIncidentAndOutboxEvent() throws Exception {
        String body = alertBody("fp-001", "firing", "order-service", "critical", "redis pool exhausted");

        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.status").value("NEW"))
                .andExpect(jsonPath("$.deduplicated").value(false))
                .andExpect(jsonPath("$.incidentId").isNotEmpty());

        List<OutboxEvent> pending = outbox.findTop50ByPublishedAtIsNullOrderByCreatedAtAsc();
        assertThat(pending).hasSize(1);
        OutboxEvent event = pending.getFirst();
        assertThat(event.getEventType()).isEqualTo("investigation.requested");
        assertThat(event.getAggregateVersion()).isEqualTo(1L);
        assertThat(event.isPublished()).isFalse();
    }

    @Test
    void outboxPayloadMatchesVersionedContract() throws Exception {
        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON)
                        .content(alertBody("fp-002", "firing", "order-service", "critical", "redis pool exhausted")))
                .andExpect(status().isAccepted());

        OutboxEvent event = outbox.findTop50ByPublishedAtIsNullOrderByCreatedAtAsc().getFirst();
        JsonNode message = mapper.readTree(event.getPayload());

        // 契约是唯一事实来源：直接用 contracts/events/v1/*.schema.json 校验生产出的消息。
        // 为让校验完全离线，先把 $ref 指向的公共 schema 内联进来。
        JsonSchema schema = loadOfflineSchema("events/v1/investigation-requested.schema.json");
        var errors = schema.validate(message);
        assertThat(errors).as("contract violations: %s", errors).isEmpty();

        assertThat(message.at("/envelope/producer").asText()).isEqualTo("control-plane");
        assertThat(message.at("/envelope/event_type").asText()).isEqualTo("investigation.requested");
        assertThat(message.at("/payload/incident_id").asText()).isNotEmpty();
        assertThat(message.at("/payload/investigation_run_id").asText()).isNotEmpty();
        // service_hint 必须等于告警服务：Agent 靠它决定调查起点
        assertThat(message.at("/payload/service_hint").asText()).isEqualTo("order-service");
    }

    private JsonSchema loadOfflineSchema(String relativePath) throws Exception {
        JsonNode requested = mapper.readTree(Files.readString(CONTRACTS.resolve(relativePath)));
        JsonNode envelope = mapper.readTree(Files.readString(
                CONTRACTS.resolve("common/event-envelope.schema.json")));
        JsonNode timeRange = mapper.readTree(Files.readString(
                CONTRACTS.resolve("common/time-range.schema.json")));

        // 内联 common/$ref（保留 schema 语义，去掉网络解析）
        var root = (com.fasterxml.jackson.databind.node.ObjectNode) requested;
        var defs = root.putObject("$defs");
        defs.set("event-envelope", stripIds(envelope));
        defs.set("time-range", stripIds(timeRange));

        inlineRef(root, "../../common/event-envelope.schema.json", "#/$defs/event-envelope");
        inlineRef(root, "../../common/time-range.schema.json", "#/$defs/time-range");

        JsonSchemaFactory factory = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012);
        return factory.getSchema(root);
    }

    private JsonNode stripIds(JsonNode node) {
        var copy = node.deepCopy();
        if (copy instanceof com.fasterxml.jackson.databind.node.ObjectNode obj) {
            obj.remove("$id");
            obj.remove("$schema");
        }
        return copy;
    }

    private void inlineRef(JsonNode node, String oldRef, String newRef) {
        if (node.isObject()) {
            var obj = (com.fasterxml.jackson.databind.node.ObjectNode) node;
            if (oldRef.equals(obj.path("$ref").asText())) {
                obj.put("$ref", newRef);
            }
            obj.elements().forEachRemaining(child -> inlineRef(child, oldRef, newRef));
        } else if (node.isArray()) {
            node.elements().forEachRemaining(child -> inlineRef(child, oldRef, newRef));
        }
    }

    @Test
    void duplicateFingerprintIsDeduplicatedWithoutSecondEvent() throws Exception {
        String body = alertBody("fp-003", "firing", "order-service", "warning", "same fault repeats");

        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.deduplicated").value(false));

        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.deduplicated").value(true));

        assertThat(outbox.findTop50ByPublishedAtIsNullOrderByCreatedAtAsc()).hasSize(1);
    }

    @Test
    void resolvedIncidentLetsSameFingerprintOpenANewOne() throws Exception {
        String body = alertBody("fp-004", "firing", "payment-service", "critical", "billing api timeout");

        // 第一次 → NEW，并去状态机里把它走完：NEW → INVESTIGATING → DIAGNOSED → RESOLVED
        String firstJson = mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.deduplicated").value(false))
                .andReturn().getResponse().getContentAsString();
        String firstId = mapper.readTree(firstJson).path("incidentId").asText();

        for (String target : List.of("INVESTIGATING", "DIAGNOSED", "RESOLVED")) {
            mvc.perform(post("/api/v1/incidents/{id}/transitions", firstId)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"target\":\"" + target + "\"}"))
                    .andExpect(status().isOk());
        }

        // 事故已终结 → 同 fingerprint 再来一次必须新建，而不是被旧事故吞掉
        String secondJson = mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.deduplicated").value(false))
                .andReturn().getResponse().getContentAsString();
        String secondId = mapper.readTree(secondJson).path("incidentId").asText();

        assertThat(secondId).isNotEqualTo(firstId);
        assertThat(outbox.findTop50ByPublishedAtIsNullOrderByCreatedAtAsc()).hasSize(2);
    }

    @Test
    void illegalTransitionReturnsConflict() throws Exception {
        String created = mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON)
                        .content(alertBody("fp-005", "firing", "inventory-service", "warning", "slow query")))
                .andExpect(status().isAccepted())
                .andReturn().getResponse().getContentAsString();
        String incidentId = mapper.readTree(created).path("incidentId").asText();

        mvc.perform(get("/api/v1/incidents/{id}", incidentId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("NEW"));

        mvc.perform(post("/api/v1/incidents/{id}/transitions", incidentId)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"target\":\"RESOLVED\"}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.from").value("NEW"))
                .andExpect(jsonPath("$.to").value("RESOLVED"));

        mvc.perform(post("/api/v1/incidents/{id}/transitions", incidentId)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"target\":\"INVESTIGATING\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("INVESTIGATING"));
    }

    @Test
    void resolvedAlertDoesNotCreateIncident() throws Exception {
        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON)
                        .content(alertBody("fp-006", "resolved", "order-service", "warning", "already recovered")))
                .andExpect(status().isBadRequest());

        assertThat(outbox.findTop50ByPublishedAtIsNullOrderByCreatedAtAsc()).isEmpty();
    }

    @Test
    void missingFingerprintIsRejected() throws Exception {
        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON)
                        .content(alertBody(null, "firing", "order-service", "warning", "no fingerprint")))
                .andExpect(status().isBadRequest());
    }
}
