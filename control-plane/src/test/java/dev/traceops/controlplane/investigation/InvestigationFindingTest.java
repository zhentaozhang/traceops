package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.LinkedHashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

/**
 * 诊断结论可查询性测试。
 *
 * <p>为什么要有：Java 侧此前能消费结果、能驱动状态机，但 REST 层看不到
 * "根因是什么、证据有多少" —— 结论只活在事件里，一次 ACK 之后就只剩账本里的一行。
 * 对一个 SRE 工具来说，这等于结论不可用。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@AutoConfigureMockMvc
@Transactional
class InvestigationFindingTest {

    @Autowired
    private MockMvc mvc;

    @Autowired
    private ObjectMapper mapper;

    @Autowired
    private InvestigationResultService service;

    private String completedEvent(String incidentId, String runId, String diagnosisStatus,
                                 String service, String faultType, int evidenceTotal) {
        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("event_id", "evt-" + runId);
        envelope.put("event_type", "investigation.completed");
        envelope.put("schema_version", "1.0");
        envelope.put("occurred_at", "2026-10-31T00:00:00Z");
        envelope.put("correlation_id", incidentId);   // 事故 ID 在信封上，不在 payload
        envelope.put("causation_id", null);
        envelope.put("aggregate_id", runId);
        envelope.put("aggregate_version", 1);
        envelope.put("producer", "agent-runtime");

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("investigation_run_id", runId);
        payload.put("diagnosis_status", diagnosisStatus);
        payload.put("rca_report_ref", runId + "/report");
        payload.put("root_cause_candidates", java.util.List.of(Map.of(
                "candidate_service", service,
                "fault_type", faultType,
                "rank", 1,
                "hypothesis_status", "supported")));
        payload.put("evidence_summary", Map.of("total", evidenceTotal));
        payload.put("usage", Map.of("tool_calls", 7, "tokens", 1234, "rounds", 2,
                "duration_seconds", 30));
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

    private String createIncidentViaAlert(String fingerprint) throws Exception {
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

    @Test
    void supportedResultIsQueryableThroughRest() throws Exception {
        String incidentId = createIncidentViaAlert("fp-finding-1");
        driveToInvestigating(incidentId);

        service.apply(completedEvent(incidentId, "run-1", "SUPPORTED",
                "redis", "pool_exhaustion", 12));

        mvc.perform(get("/api/v1/incidents/{id}/finding", incidentId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.diagnosisStatus").value("SUPPORTED"))
                .andExpect(jsonPath("$.rootCauseService").value("redis"))
                .andExpect(jsonPath("$.rootCauseFaultType").value("pool_exhaustion"))
                .andExpect(jsonPath("$.evidenceTotal").value(12))
                .andExpect(jsonPath("$.toolCalls").value(7))
                .andExpect(jsonPath("$.needsHumanReview").value(false))
                .andExpect(jsonPath("$.reportRef").value("run-1/report"));
    }

    @Test
    void inconclusiveResultIsAlsoStoredWithHumanReviewFlag() throws Exception {
        String incidentId = createIncidentViaAlert("fp-finding-2");
        driveToInvestigating(incidentId);

        service.apply(completedEvent(incidentId, "run-2", "INCONCLUSIVE",
                "unknown", "unknown", 3));

        mvc.perform(get("/api/v1/incidents/{id}/finding", incidentId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.diagnosisStatus").value("INCONCLUSIVE"))
                .andExpect(jsonPath("$.abstained").value(true))
                // 证据不足本身就是结论，必须能查到"为什么没定论"
                .andExpect(jsonPath("$.needsHumanReview").value(true))
                .andExpect(jsonPath("$.evidenceTotal").value(3));
    }

    @Test
    void missingFindingReturnsNotFound() throws Exception {
        String incidentId = createIncidentViaAlert("fp-finding-3");

        mvc.perform(get("/api/v1/incidents/{id}/finding", incidentId))
                .andExpect(status().isNotFound());
    }

    @Test
    void findingIsReplacedByLaterRunNotDuplicated() throws Exception {
        String incidentId = createIncidentViaAlert("fp-finding-4");
        driveToInvestigating(incidentId);

        service.apply(completedEvent(incidentId, "run-a", "INCONCLUSIVE", "unknown", "unknown", 2));
        // 第二轮重跑给出结论：结论应被更新，而不是留下两条
        service.apply(completedEvent(incidentId, "run-b", "SUPPORTED", "redis", "pool_exhaustion", 9));

        mvc.perform(get("/api/v1/incidents/{id}/finding", incidentId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.investigationRunId").value("run-b"))
                .andExpect(jsonPath("$.diagnosisStatus").value("SUPPORTED"))
                .andExpect(jsonPath("$.rootCauseService").value("redis"))
                .andExpect(jsonPath("$.evidenceTotal").value(9));
    }
}
