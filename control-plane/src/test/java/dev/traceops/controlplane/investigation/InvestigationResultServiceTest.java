package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.domain.IncidentStatus;
import java.time.Instant;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.transaction.annotation.Transactional;

/**
 * 回程消费的真实行为测试：真实 H2 + 真实状态机，不 mock 被测核心逻辑。
 *
 * <p>刻意不用 {@code @MockBean}：本任务的核心正是"事务 + 状态机 + 幂等账本"三者的配合，
 * 把它们 mock 掉就只剩下断言 mock 的返回值，等于没测。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@Transactional
class InvestigationResultServiceTest {

    @Autowired
    private InvestigationResultService service;

    @Autowired
    private IncidentRepository incidents;

    @Autowired
    private AppliedInvestigationEventRepository appliedEvents;

    @Autowired
    private ObjectMapper mapper;

    @Autowired
    private jakarta.persistence.EntityManager entityManager;

    private String incidentId;

    @BeforeEach
    void setUp() {
        // 直接落一个 NEW 事故：本测试只关心回程，不需要再走一遍告警接入
        incidentId = "inc-" + UUID.randomUUID();
        incidents.save(Incident.open(incidentId, "fp-" + incidentId, "order-service",
                "redis pool exhausted", "critical", Instant.now()));
    }

    // ---------- 构造契约合法消息 ----------

    private ObjectNode completedPayload(String runId, String diagnosisStatus) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("investigation_run_id", runId);
        payload.put("diagnosis_status", diagnosisStatus);
        payload.put("rca_report_ref", runId + "/report");
        ObjectNode candidate = payload.putArray("root_cause_candidates").addObject();
        candidate.put("candidate_service", "redis");
        candidate.put("candidate_component", "redis-connection-pool");
        candidate.put("fault_type", "pool_exhaustion");
        candidate.put("rank", 1);
        candidate.put("hypothesis_status", "supported");
        ObjectNode summary = payload.putObject("evidence_summary");
        summary.put("total", 7);
        summary.put("metrics", 2);
        summary.put("logs", 3);
        summary.put("traces", 2);
        ObjectNode usage = payload.putObject("usage");
        usage.put("tool_calls", 12);
        usage.put("tokens", 4200);
        usage.put("rounds", 3);
        usage.put("duration_seconds", 95);
        payload.put("abstained", false);
        return payload;
    }

    private ObjectNode failedPayload(String runId, String reason, boolean retryable) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("investigation_run_id", runId);
        payload.put("terminal_status", "FAILED");
        payload.put("failure_reason", reason);
        payload.put("retryable", retryable);
        payload.put("failed_node", "hypothesis_generator");
        payload.put("checkpoint_id", "ckpt-" + runId);
        ObjectNode usage = payload.putObject("usage_at_failure");
        usage.put("tool_calls", 5);
        usage.put("tokens", 1500);
        usage.put("rounds", 2);
        return payload;
    }

    /** 契约事实：payload 没有 incident_id，事故 ID 走信封 correlation_id。 */
    private String envelope(String eventType, String eventId, String runId, String producer,
            String correlationId) {
        ObjectNode envelope = mapper.createObjectNode();
        envelope.put("event_id", eventId);
        envelope.put("event_type", eventType);
        envelope.put("schema_version", "1.0");
        envelope.put("occurred_at", Instant.now().toString());
        envelope.put("correlation_id", correlationId);
        envelope.putNull("causation_id");
        envelope.put("aggregate_id", runId);
        envelope.put("aggregate_version", 1);
        envelope.put("producer", producer);
        return envelope.toString();
    }

    private String completed(String eventId, String runId, String diagnosisStatus) {
        return message(envelope("investigation.completed", eventId, runId, "agent-runtime", incidentId),
                completedPayload(runId, diagnosisStatus));
    }

    private String failed(String eventId, String runId, String reason, boolean retryable) {
        return message(envelope("investigation.failed", eventId, runId, "agent-runtime", incidentId),
                failedPayload(runId, reason, retryable));
    }

    private String message(String envelopeJson, ObjectNode payload) {
        return "{\"envelope\":" + envelopeJson + ",\"payload\":" + payload + "}";
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

    /** 把事故推进到指定起点（用真实状态机，不直接 setStatus）。 */
    private void moveTo(IncidentStatus... steps) {
        Incident incident = incidents.findById(incidentId).orElseThrow();
        for (IncidentStatus step : steps) {
            incident.transitionTo(step, Instant.now());
        }
        incidents.save(incident);
    }

    // ---------- 1. SUPPORTED ----------

    @Test
    void supportedResultDrivesIncidentFromNewToDiagnosed() {
        assertThat(currentStatus()).isEqualTo(IncidentStatus.NEW);

        Applied applied = service.apply(completed(evt(), run(), "SUPPORTED"));

        // NEW → INVESTIGATING → DIAGNOSED：状态机不允许 NEW 直跳 DIAGNOSED，
        // 所以服务必须连续转两次
        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.APPLIED);
        assertThat(applied.previousStatus()).isEqualTo(IncidentStatus.NEW);
        assertThat(applied.resultingStatus()).isEqualTo(IncidentStatus.DIAGNOSED);
        assertThat(currentStatus()).isEqualTo(IncidentStatus.DIAGNOSED);
        // 回程内容被真正解析出来，而不是只看了个 event_type
        assertThat(applied.reportRef()).isEqualTo(applied.investigationRunId() + "/report");
        assertThat(applied.diagnosisStatus()).isEqualTo(InvestigationResult.DiagnosisStatus.SUPPORTED);
    }

    @Test
    void supportedResultFromInvestigatingAlsoReachesDiagnosed() {
        moveTo(IncidentStatus.INVESTIGATING);

        Applied applied = service.apply(completed(evt(), run(), "SUPPORTED"));

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.APPLIED);
        assertThat(currentStatus()).isEqualTo(IncidentStatus.DIAGNOSED);
    }

    @Test
    void parsedResultCarriesCandidatesAndUsage() {
        String runId = run();
        InvestigationResult parsed = InvestigationResult.fromJson(
                nodes(completed(evt(), runId, "SUPPORTED")));

        assertThat(parsed.incidentId()).isEqualTo(incidentId);
        assertThat(parsed.rootCauseCandidates()).hasSize(1);
        assertThat(parsed.rootCauseCandidates().getFirst().service()).isEqualTo("redis");
        assertThat(parsed.rootCauseCandidates().getFirst().rank()).isEqualTo(1);
        assertThat(parsed.usage().toolCalls()).isEqualTo(12);
        assertThat(parsed.usage().tokens()).isEqualTo(4200);
        assertThat(parsed.evidenceSummary().total()).isEqualTo(7);
        assertThat(parsed.abstained()).isFalse();
    }

    private com.fasterxml.jackson.databind.JsonNode nodes(String json) {
        try {
            return mapper.readTree(json);
        } catch (Exception ex) {
            throw new IllegalStateException(ex);
        }
    }

    // ---------- 2/3. INCONCLUSIVE / REFUTED 不推进 ----------

    @Test
    void inconclusiveResultDoesNotAdvanceAndFlagsHumanReview() {
        moveTo(IncidentStatus.INVESTIGATING);
        assertThat(currentStatus()).isEqualTo(IncidentStatus.INVESTIGATING);

        Applied applied = service.apply(completed(evt(), run(), "INCONCLUSIVE"));

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.NEEDS_HUMAN_REVIEW);
        assertThat(applied.needsHumanReview()).isTrue();
        assertThat(applied.diagnosisStatus()).isEqualTo(InvestigationResult.DiagnosisStatus.INCONCLUSIVE);
        // 关键断言：INCONCLUSIVE 不能被当成"诊断完成"
        assertThat(currentStatus()).isEqualTo(IncidentStatus.INVESTIGATING);
        assertThat(currentStatus()).isNotEqualTo(IncidentStatus.DIAGNOSED);
    }

    @Test
    void refutedResultDoesNotAdvanceAndFlagsHumanReview() {
        moveTo(IncidentStatus.INVESTIGATING);

        Applied applied = service.apply(completed(evt(), run(), "REFUTED"));

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.NEEDS_HUMAN_REVIEW);
        assertThat(applied.needsHumanReview()).isTrue();
        assertThat(applied.diagnosisStatus()).isEqualTo(InvestigationResult.DiagnosisStatus.REFUTED);
        assertThat(currentStatus()).isEqualTo(IncidentStatus.INVESTIGATING);
    }

    @Test
    void inconclusiveOnNewIncidentKeepsItNew() {
        Applied applied = service.apply(completed(evt(), run(), "INCONCLUSIVE"));

        assertThat(applied.needsHumanReview()).isTrue();
        // 也不能顺手补一个 INVESTIGATING：没推进就是没推进
        assertThat(currentStatus()).isEqualTo(IncidentStatus.NEW);
    }

    // ---------- 4/5. failed ----------

    @Test
    void retryableFailureKeepsIncidentInvestigatingAndMarksRetryable() {
        moveTo(IncidentStatus.INVESTIGATING);

        Applied applied = service.apply(failed(evt(), run(), "MCP_SERVER_UNAVAILABLE", true));

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.RETRY_PENDING);
        assertThat(applied.retryable()).isTrue();
        assertThat(currentStatus()).isEqualTo(IncidentStatus.INVESTIGATING);
    }

    @Test
    void retryableFailureOnNewIncidentRecordsInvestigatingWithoutFailing() {
        Applied applied = service.apply(failed(evt(), run(), "BUDGET_EXHAUSTED", true));

        assertThat(applied.retryable()).isTrue();
        // 可重试失败不代表事故结束，更不能转 FAILED
        assertThat(currentStatus()).isEqualTo(IncidentStatus.INVESTIGATING);
        assertThat(currentStatus()).isNotEqualTo(IncidentStatus.FAILED);
    }

    @Test
    void nonRetryableFailureMovesIncidentToFailed() {
        moveTo(IncidentStatus.INVESTIGATING);

        Applied applied = service.apply(failed(evt(), run(), "DATASOURCE_MISSING", false));

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.APPLIED);
        assertThat(applied.retryable()).isFalse();
        assertThat(currentStatus()).isEqualTo(IncidentStatus.FAILED);
    }

    @Test
    void failurePayloadFieldsAreParsed() {
        String runId = run();
        InvestigationResult parsed = InvestigationResult.fromJson(nodes(failed(evt(), runId, "DEADLINE_EXCEEDED", false)));

        assertThat(parsed.terminalStatus()).isEqualTo(InvestigationResult.TerminalStatus.FAILED);
        assertThat(parsed.failureReason()).isEqualTo(InvestigationResult.FailureReason.DEADLINE_EXCEEDED);
        assertThat(parsed.runTerminalStatus()).isEqualTo(InvestigationResult.RunTerminalStatus.FAILED);
        assertThat(parsed.retryable()).isFalse();
        assertThat(parsed.failedNode()).isEqualTo("hypothesis_generator");
        assertThat(parsed.checkpointId()).isEqualTo("ckpt-" + runId);
        assertThat(parsed.usage().toolCalls()).isEqualTo(5);
    }

    // ---------- 6. 幂等 ----------

    @Test
    void duplicateEventIdIsAppliedOnlyOnce() {
        String eventId = evt();
        String runId = run();

        Applied first = service.apply(completed(eventId, runId, "SUPPORTED"));
        assertThat(first.outcome()).isEqualTo(Applied.Outcome.APPLIED);
        assertThat(currentStatus()).isEqualTo(IncidentStatus.DIAGNOSED);

        Applied second = service.apply(completed(eventId, runId, "SUPPORTED"));

        assertThat(second.outcome()).isEqualTo(Applied.Outcome.SKIPPED_DUPLICATE);
        // 第二次没有再次驱动状态机：仍是 DIAGNOSED，而且只留了一条账
        assertThat(currentStatus()).isEqualTo(IncidentStatus.DIAGNOSED);
        assertThat(appliedEvents.count()).isEqualTo(1);
    }

    @Test
    void reusedEventIdWithDifferentRunIsAlsoSkipped() {
        // 上游重发可能重新生成 event_id：同一 Run 仍必须只结算一次
        String runId = run();
        service.apply(completed(evt(), runId, "SUPPORTED"));

        Applied second = service.apply(completed(evt(), runId, "SUPPORTED"));

        assertThat(second.outcome()).isEqualTo(Applied.Outcome.SKIPPED_DUPLICATE);
        assertThat(currentStatus()).isEqualTo(IncidentStatus.DIAGNOSED);
        assertThat(appliedEvents.count()).isEqualTo(1);
    }

    @Test
    void duplicateOfHumanReviewEventReportsSameSignal() {
        String eventId = evt();
        String runId = run();
        moveTo(IncidentStatus.INVESTIGATING);

        service.apply(completed(eventId, runId, "INCONCLUSIVE"));
        Applied second = service.apply(completed(eventId, runId, "INCONCLUSIVE"));

        // 重复到达也要保持"需人工"的信号，不能因为跳过而假装一切都好
        assertThat(second.outcome()).isEqualTo(Applied.Outcome.SKIPPED_DUPLICATE);
        assertThat(second.needsHumanReview()).isTrue();
    }

    @Test
    void runIdUniqueConstraintIsEnforcedByDatabase() {
        // 幂等预检是常规路径，数据库唯一约束是并发窗口里的最后防线。
        // 这里绕过 service 直接写两条同 run_id 的账，证明约束真的存在（不是只写在注释里）。
        String runId = run();
        appliedEvents.saveAndFlush(AppliedInvestigationEvent.of(evt(), runId, "investigation.completed",
                incidentId, "APPLIED", "NEW", "DIAGNOSED", null, Instant.now()));

        assertThatThrownBy(() -> appliedEvents.saveAndFlush(AppliedInvestigationEvent.of(evt(), runId,
                        "investigation.completed", incidentId, "APPLIED", "NEW", "DIAGNOSED", null, Instant.now())))
                .isInstanceOf(org.springframework.dao.DataIntegrityViolationException.class);
        // 失败的 flush 会在会话里留下脏对象，不清掉的话后面任何查询都会再次触发它
        entityManager.clear();
        assertThat(appliedEvents.count()).isEqualTo(1);
    }

    // ---------- 7/8. 身份校验 ----------
    @Test
    void nonAgentRuntimeProducerIsRejected() {
        String message = message(
                envelope("investigation.completed", evt(), run(), "control-plane", incidentId),
                completedPayload(run(), "SUPPORTED"));

        assertThatThrownBy(() -> service.apply(message))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("producer");
        // 被拒的消息不得推进状态机，也不得留下任何账
        assertThat(currentStatus()).isEqualTo(IncidentStatus.NEW);
        assertThat(appliedEvents.count()).isZero();
    }

    @Test
    void wrongEventTypeIsRejected() {
        String message = message(
                envelope("investigation.requested", evt(), run(), "agent-runtime", incidentId),
                completedPayload(run(), "SUPPORTED"));

        assertThatThrownBy(() -> service.apply(message))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("event_type");
        assertThat(currentStatus()).isEqualTo(IncidentStatus.NEW);
    }

    // ---------- 9. incident 不存在 ----------

    @Test
    void unknownIncidentIsRecordedAsNotFoundWithoutThrowing() {
        String unknownId = "inc-does-not-exist-" + UUID.randomUUID();
        String message = message(
                envelope("investigation.completed", evt(), run(), "agent-runtime", unknownId),
                completedPayload(run(), "SUPPORTED"));

        Applied applied = service.apply(message);

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.NOT_FOUND);
        assertThat(applied.incidentId()).isEqualTo(unknownId);
        assertThat(applied.reason()).contains("incident not found");
        // 账本落一条 NOT_FOUND：同样的消息再来一次仍然跳过，而不是反复失败
        assertThat(appliedEvents.count()).isEqualTo(1);
        assertThat(appliedEvents.findAll().getFirst().getOutcome()).isEqualTo("NOT_FOUND");
    }

    // ---------- 10. 陈旧消息 ----------

    @Test
    void completedResultForResolvedIncidentIsRejectedNotThrown() {
        moveTo(IncidentStatus.INVESTIGATING, IncidentStatus.DIAGNOSED, IncidentStatus.RESOLVED);

        Applied applied = service.apply(completed(evt(), run(), "SUPPORTED"));

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.REJECTED);
        assertThat(applied.reason()).contains("illegal transition");
        assertThat(applied.resultingStatus()).isEqualTo(IncidentStatus.RESOLVED);
        // 终态不被陈旧消息改写
        assertThat(currentStatus()).isEqualTo(IncidentStatus.RESOLVED);
        assertThat(appliedEvents.count()).isEqualTo(1);
    }

    @Test
    void staleFailureForResolvedIncidentIsRejectedNotThrown() {
        moveTo(IncidentStatus.INVESTIGATING, IncidentStatus.DIAGNOSED, IncidentStatus.RESOLVED);

        Applied applied = service.apply(failed(evt(), run(), "INTERNAL_ERROR", false));

        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.REJECTED);
        assertThat(currentStatus()).isEqualTo(IncidentStatus.RESOLVED);
    }

    // ---------- 契约解析的硬校验 ----------

    @Test
    void malformedJsonIsRejectedWithClearError() {
        assertThatThrownBy(() -> service.apply("not-json"))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("not valid JSON");
    }

    @Test
    void missingDiagnosisStatusIsRejectedInsteadOfDefaulted() {
        String runId = run();
        ObjectNode payload = mapper.createObjectNode();
        payload.put("investigation_run_id", runId);
        payload.put("rca_report_ref", runId + "/report");
        String message = message(
                envelope("investigation.completed", evt(), runId, "agent-runtime", incidentId), payload);

        // 缺 diagnosis_status 时不能默认成 SUPPORTED 去推进状态机
        assertThatThrownBy(() -> service.apply(message))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("diagnosis_status");
        assertThat(currentStatus()).isEqualTo(IncidentStatus.NEW);
    }

    @Test
    void unknownFailureReasonIsRejected() {
        String runId = run();
        ObjectNode payload = failedPayload(runId, "FREE_TEXT_OK", false);
        String message = message(
                envelope("investigation.failed", evt(), runId, "agent-runtime", incidentId), payload);

        assertThatThrownBy(() -> service.apply(message))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("failure_reason");
    }

    // ---------- SUPPORTED 之后的下游不受影响 ----------

    @Test
    void diagnosedIncidentCanContinueNormalDownstreamFlow() {
        service.apply(completed(evt(), run(), "SUPPORTED"));
        assertThat(currentStatus()).isEqualTo(IncidentStatus.DIAGNOSED);

        // 只有真到达 DIAGNOSED，下游的处置边才可达；这也反证了服务没绕过状态机
        moveTo(IncidentStatus.RESOLVED);

        assertThat(currentStatus()).isEqualTo(IncidentStatus.RESOLVED);
    }

    @Test
    void payloadIncidentIdIsHonouredWhenPresent() {
        // 契约里事故 ID 走 correlation_id；若发送方额外带了 payload.incident_id，
        // 应当优先采用它，而不是静默用信封里那个可能被复制错的关联 ID。
        String otherIncident = "inc-other-" + UUID.randomUUID();
        incidents.save(Incident.open(otherIncident, "fp-" + otherIncident, "payment-service",
                "billing timeout", "critical", Instant.now()));
        String runId = run();
        ObjectNode payload = completedPayload(runId, "SUPPORTED");
        payload.put("incident_id", otherIncident);
        String message = message(
                envelope("investigation.completed", evt(), runId, "agent-runtime", incidentId), payload);

        Applied applied = service.apply(message);

        assertThat(applied.incidentId()).isEqualTo(otherIncident);
        assertThat(incidents.findById(otherIncident).orElseThrow().getStatus())
                .isEqualTo(IncidentStatus.DIAGNOSED);
        // 信封里那个事故不受影响
        assertThat(currentStatus()).isEqualTo(IncidentStatus.NEW);
    }

    // ---------- 测试消息本身必须真的是契约合法消息 ----------

    @Test
    void completedMessageMatchesTheVersionedContract() throws Exception {
        // 如果测试构造的消息不符合契约，那么"服务能处理它"证明的是服务能处理假消息。
        // 用与 AlertIngestIT 相同的离线内联方式校验，保证测试输入来自真正的事实来源。
        assertThat(validateAgainstSchema("events/v1/investigation-completed.schema.json",
                completed(evt(), run(), "SUPPORTED"))).isEmpty();
    }

    @Test
    void failedMessageMatchesTheVersionedContract() throws Exception {
        assertThat(validateAgainstSchema("events/v1/investigation-failed.schema.json",
                failed(evt(), run(), "MCP_SERVER_UNAVAILABLE", true))).isEmpty();
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
