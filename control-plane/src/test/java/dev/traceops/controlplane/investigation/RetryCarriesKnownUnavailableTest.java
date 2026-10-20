package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.domain.IncidentStatus;
import dev.traceops.controlplane.outbox.OutboxEvent;
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
 * 重试必须知道上一轮哪些数据源够不到。
 *
 * <p>被替换的缺陷：重试只带"重做同一个事故、同一个时间窗"，从不带"上轮缺了什么"。
 * 于是 Agent 的新一轮仍按固定顺序收集，预算仍然先花在上一轮已经成功的源上，
 * 轮到 loki 时预算又没了 —— 重试复现了同一次失败，报告还会把它记成"一次新的、
 * 同样 INCONCLUSIVE 的调查"。两次失败看起来像两次独立事件，实际上是同一个原因。
 *
 * <p>回路：{@code investigation.completed.failed_sources} → 事故记住 →
 * 下一次 {@code investigation.requested} 带 {@code retry_of} + {@code known_unavailable}
 * → Agent 先查这些源。四段都在这里断言，缺任何一段这条回路就是断的。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@Transactional
class RetryCarriesKnownUnavailableTest {

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

    private ObjectNode completedPayload(String runId, String... failedSources) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("investigation_run_id", runId);
        // INCONCLUSIVE：证据不足。本轮没下结论，但"哪几个源够不到"是另一件事，
        // 必须照样传下去 —— 否则下一轮永远不知道该怎么把证据补齐。
        payload.put("diagnosis_status", "INCONCLUSIVE");
        payload.put("rca_report_ref", runId + "/report");
        payload.putArray("root_cause_candidates");
        ObjectNode summary = payload.putObject("evidence_summary");
        summary.put("total", 4);
        summary.put("metrics", 4);
        ObjectNode usage = payload.putObject("usage");
        usage.put("tool_calls", 6);
        usage.put("tokens", 900);
        usage.put("rounds", 2);
        usage.put("duration_seconds", 40);
        payload.put("abstained", true);
        var sources = payload.putArray("failed_sources");
        for (String source : failedSources) {
            sources.add(source);
        }
        return payload;
    }

    private ObjectNode failedPayload(String runId) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("investigation_run_id", runId);
        payload.put("terminal_status", "FAILED");
        payload.put("failure_reason", "MCP_SERVER_UNAVAILABLE");
        payload.put("retryable", true);
        payload.put("failed_node", "collect");
        payload.put("checkpoint_id", "ckpt-" + runId);
        return payload;
    }

    private String message(String eventType, String eventId, String runId, ObjectNode payload) {
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("event_id", eventId);
        envelope.put("event_type", eventType);
        envelope.put("schema_version", "1.0");
        envelope.put("occurred_at", Instant.now().toString());
        envelope.put("correlation_id", incidentId);
        envelope.putNull("causation_id");
        envelope.put("aggregate_id", runId);
        envelope.put("aggregate_version", 1);
        envelope.put("producer", "agent-runtime");
        return "{\"envelope\":" + envelope + ",\"payload\":" + payload + "}";
    }

    private static String evt() {
        return "evt-" + UUID.randomUUID();
    }

    private static String run() {
        return "run-" + UUID.randomUUID();
    }

    /** 模拟控制面为某一轮发了请求：事故据此记住"当前在哪一轮"。 */
    private void theRunIsInFlight(String runId) {
        Incident incident = incidents.findById(incidentId).orElseThrow();
        incident.beginRun(runId);
        incidents.save(incident);
    }

    private Instant now() {
        return Instant.now();
    }

    @SuppressWarnings("unchecked")
    private List<Map<String, Object>> retryRequestPayloads() {
        List<Map<String, Object>> payloads = new java.util.ArrayList<>();
        for (OutboxEvent event : outbox.findAll()) {
            if (!"investigation.requested".equals(event.getEventType())) {
                continue;
            }
            try {
                Map<String, Object> message = mapper.readValue(event.getPayload(), Map.class);
                payloads.add((Map<String, Object>) message.get("payload"));
            } catch (Exception ex) {
                throw new IllegalStateException(ex);
            }
        }
        return payloads;
    }

    private Map<String, Object> theOnlyRetryPayload() {
        List<Map<String, Object>> payloads = retryRequestPayloads();
        assertThat(payloads).hasSize(1);
        return payloads.getFirst();
    }

    // ---------- 1. 完成事件里的失败源落在事故上 ----------

    @Test
    void aCompletedRunRemembersWhichSourcesItCouldNotReach() {
        resultService.apply(message("investigation.completed", evt(), run(),
                completedPayload("run-done", "loki", "tempo")));

        Incident incident = incidents.findById(incidentId).orElseThrow();
        assertThat(incident.getUnavailableSources()).containsExactly("loki", "tempo");
    }

    @Test
    void duplicateAndPaddedSourcesAreCollapsed() {
        // 空白元素在解析阶段就被拒（见 blankSourceNamesAreRejected），
        // 这里只管"重复"与"带空格"两种噪声。
        resultService.apply(message("investigation.completed", evt(), run(),
                completedPayload("run-done", "loki", "loki ", "tempo")));

        // 去重 + 排序：两次重试的取值可以直接用 equals 比对，而不是靠肉眼
        assertThat(incidents.findById(incidentId).orElseThrow().getUnavailableSources())
                .containsExactly("loki", "tempo");
    }

    @Test
    void aRunThatReachedEverythingClearsTheOldList() {
        resultService.apply(message("investigation.completed", evt(), run(),
                completedPayload("run-1", "loki")));
        assertThat(incidents.findById(incidentId).orElseThrow().getUnavailableSources())
                .containsExactly("loki");

        // 空列表 ≠ "不知道"：这一轮全都够得到，是必须覆盖旧值的信息。
        // 保留旧的失败源会让下一次重试白跑一趟。
        resultService.apply(message("investigation.completed", evt(), run(), completedPayload("run-2")));

        assertThat(incidents.findById(incidentId).orElseThrow().getUnavailableSources()).isEmpty();
    }

    // ---------- 2. 重试请求把它们带给 Agent ----------

    @Test
    void theRetryRequestCarriesTheSourcesThatWereUnreachable() {
        resultService.apply(message("investigation.completed", evt(), run(),
                completedPayload("run-done", "loki")));

        resultService.apply(message("investigation.failed", evt(), run(), failedPayload("run-failed")));

        Map<String, Object> payload = theOnlyRetryPayload();
        assertThat(payload.get("known_unavailable")).isEqualTo(List.of("loki"));
    }

    @Test
    void theRetryRequestSaysWhichRunItRedoes() {
        theRunIsInFlight("run-previous");

        resultService.apply(message("investigation.failed", evt(), run(), failedPayload("run-failed")));

        // 没有 retry_of，"同一事故的第三次尝试"与"三次互不相关的调查"在事件流里无法区分
        assertThat(theOnlyRetryPayload().get("retry_of")).isEqualTo("run-previous");
    }

    @Test
    void aFirstAttemptCarriesNoRetryContext() {
        // 事故从未开始过任何一轮：retry_of 与 known_unavailable 都不该出现，
        // 而不是写成空串/空数组 —— 那会让"没有上一轮"和"上一轮什么都没有"混起来。
        resultService.apply(message("investigation.failed", evt(), run(), failedPayload("run-failed")));

        Map<String, Object> payload = theOnlyRetryPayload();
        assertThat(payload).doesNotContainKey("retry_of");
        assertThat(payload).doesNotContainKey("known_unavailable");
    }

    @Test
    void theRetryRequestIsStillAValidContractMessage() {
        // 端到端要过 ContractValidator：新字段写错名字会被这一步拒掉
        // （契约里 additionalProperties 为 false）。
        resultService.apply(message("investigation.completed", evt(), run(),
                completedPayload("run-done", "loki")));
        resultService.apply(message("investigation.failed", evt(), run(), failedPayload("run-failed")));

        assertThat(retryRequestPayloads()).hasSize(1);
    }

    // ---------- 3. 坏消息被拒收，而不是被猜着用 ----------

    @Test
    void failedSourcesMustBeAnArrayOfStrings() {
        ObjectNode payload = completedPayload("run-done");
        payload.put("failed_sources", "loki");

        assertThatThrownBy(() -> resultService.apply(
                message("investigation.completed", evt(), run(), payload)))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("expected an array");
    }

    @Test
    void blankSourceNamesAreRejected() {
        ObjectNode payload = completedPayload("run-done");
        payload.putArray("failed_sources").add("   ");

        assertThatThrownBy(() -> resultService.apply(
                message("investigation.completed", evt(), run(), payload)))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("non-blank string");
    }
}
