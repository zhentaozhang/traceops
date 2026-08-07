package dev.traceops.controlplane.investigation;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.ArrayList;
import java.util.List;

/**
 * agent-runtime → control-plane 终态事件的不可变解析结果（契约 v1）。
 *
 * <p>为什么解析工厂 {@link #fromJson} 挂在这个 record 上、而不是放在 service 里：
 * 本类型的字段与 {@code contracts/events/v1/investigation-completed.schema.json} /
 * {@code investigation-failed.schema.json} 一一对应，把"线上 JSON → Java 类型"的映射
 * 贴近类型本身，契约改字段时只需要动一个文件；service 只负责编排
 * （身份校验、幂等、状态机），不掺解析细节。
 *
 * <p>注意契约事实：completed / failed 的 payload <b>都没有</b> {@code incident_id} 字段
 * （payload 是 {@code additionalProperties:false}）。事故 ID 走信封的
 * {@code correlation_id}——contracts/README.md 写明它"通常为 incident_id"，
 * agent-runtime 的 {@code _envelope()} 也正是这样填的。因此这里从信封读取，
 * 而不是为了迁就某段教程文本去发明一个契约里不存在的 payload 字段
 * （若发送方额外带了 {@code payload.incident_id} 则优先采用，见 {@link #resolveIncidentId}）。
 */
public record InvestigationResult(
        String eventId,
        String investigationRunId,
        String incidentId,
        TerminalStatus terminalStatus,
        DiagnosisStatus diagnosisStatus,
        List<RootCauseCandidate> rootCauseCandidates,
        FailureReason failureReason,
        RunTerminalStatus runTerminalStatus,
        boolean retryable,
        boolean abstained,
        Usage usage,
        String reportRef,
        String failedNode,
        String checkpointId,
        EvidenceSummary evidenceSummary) {

    public InvestigationResult {
        // 可选字段缺省时空集合/零值，避免调用方到处判 null；不做"猜值"，只补"无值"的默认。
        rootCauseCandidates = rootCauseCandidates == null ? List.of() : List.copyOf(rootCauseCandidates);
        usage = usage == null ? Usage.empty() : usage;
        evidenceSummary = evidenceSummary == null ? EvidenceSummary.empty() : evidenceSummary;
    }

    /** 信封上的 event_type。 */
    public String eventType() {
        return terminalStatus.eventType();
    }

    /**
     * 严格解析：缺字段、枚举越界都抛 {@link IllegalArgumentException}，不做"尽力而为"的降级。
     * 消费端宁可把坏消息暴露出来（死信/告警），也不能静默地把错误结论应用到状态机。
     */
    public static InvestigationResult fromJson(JsonNode root) {
        if (root == null || !root.isObject()) {
            throw new IllegalArgumentException("investigation result must be a JSON object with envelope and payload");
        }
        JsonNode envelope = requiredObject(root, "envelope");
        JsonNode payload = requiredObject(root, "payload");

        String eventType = requiredText(envelope, "event_type");
        TerminalStatus terminalStatus = TerminalStatus.fromEventType(eventType);

        String eventId = requiredText(envelope, "event_id");
        String investigationRunId = requiredText(payload, "investigation_run_id");
        String incidentId = resolveIncidentId(envelope, payload);

        return switch (terminalStatus) {
            case COMPLETED -> new InvestigationResult(
                    eventId,
                    investigationRunId,
                    incidentId,
                    terminalStatus,
                    DiagnosisStatus.fromWire(requiredText(payload, "diagnosis_status")),
                    RootCauseCandidate.listFrom(payload.path("root_cause_candidates")),
                    null,
                    null,
                    false,
                    payload.path("abstained").asBoolean(false),
                    Usage.fromWire(payload.path("usage")),
                    requiredText(payload, "rca_report_ref"),
                    null,
                    null,
                    EvidenceSummary.fromWire(payload.path("evidence_summary")));
            case FAILED -> new InvestigationResult(
                    eventId,
                    investigationRunId,
                    incidentId,
                    terminalStatus,
                    null,
                    List.of(),
                    FailureReason.fromWire(requiredText(payload, "failure_reason")),
                    RunTerminalStatus.fromWire(requiredText(payload, "terminal_status")),
                    // retryable 是 failed 的必填字段：缺了就无法决定"重跑还是转 FAILED"，
                    // 宁可拒收也不要默认成 false 把可重试的 run 钉死。
                    requiredBoolean(payload, "retryable"),
                    payload.path("abstained").asBoolean(false),
                    Usage.fromWire(payload.path("usage_at_failure")),
                    null,
                    optionalText(payload, "failed_node"),
                    optionalText(payload, "checkpoint_id"),
                    EvidenceSummary.fromWire(payload.path("evidence_summary")));
        };
    }

    /** 终态类型。事件类型字符串是契约里的枚举值，这里只做映射。 */
    public enum TerminalStatus {
        COMPLETED("investigation.completed"),
        FAILED("investigation.failed");

        private final String eventType;

        TerminalStatus(String eventType) {
            this.eventType = eventType;
        }

        public String eventType() {
            return eventType;
        }

        public static boolean isTerminalEventType(String eventType) {
            return COMPLETED.eventType.equals(eventType) || FAILED.eventType.equals(eventType);
        }

        public static TerminalStatus fromEventType(String eventType) {
            for (TerminalStatus status : values()) {
                if (status.eventType.equals(eventType)) {
                    return status;
                }
            }
            // 例如 investigation.requested / investigation.updated 不是回程终态事件
            throw new IllegalArgumentException("unsupported terminal event_type: " + eventType);
        }
    }

    /** 诊断结论类型：INCONCLUSIVE 是合法交付物（证据不足），不是错误。 */
    public enum DiagnosisStatus {
        SUPPORTED,
        REFUTED,
        INCONCLUSIVE;

        public static DiagnosisStatus fromWire(String value) {
            for (DiagnosisStatus status : values()) {
                if (status.name().equals(value)) {
                    return status;
                }
            }
            throw new IllegalArgumentException("unsupported diagnosis_status: " + value);
        }
    }

    /** 失败原因（契约枚举 12 值）。不接受自由文本，否则失败统计无法聚合。 */
    public enum FailureReason {
        BUDGET_EXHAUSTED,
        DEADLINE_EXCEEDED,
        MODEL_UNAVAILABLE,
        MODEL_TIMEOUT,
        MCP_SERVER_UNAVAILABLE,
        DATASOURCE_QUERY_ERROR,
        DATASOURCE_MISSING,
        EVIDENCE_WRITE_ERROR,
        CHECKPOINT_RESTORE_FAILED,
        INVALID_REQUEST,
        CANCELLED_BY_USER,
        INTERNAL_ERROR;

        public static FailureReason fromWire(String value) {
            for (FailureReason reason : values()) {
                if (reason.name().equals(value)) {
                    return reason;
                }
            }
            throw new IllegalArgumentException("unsupported failure_reason: " + value);
        }
    }

    /** Run 自身终态（failed 事件里区分 FAILED / CANCELLED）。 */
    public enum RunTerminalStatus {
        FAILED,
        CANCELLED;

        public static RunTerminalStatus fromWire(String value) {
            for (RunTerminalStatus status : values()) {
                if (status.name().equals(value)) {
                    return status;
                }
            }
            throw new IllegalArgumentException("unsupported terminal_status: " + value);
        }
    }

    /** 候选根因：契约字段是 candidate_service / candidate_component。 */
    public record RootCauseCandidate(
            String service,
            String component,
            String faultType,
            int rank,
            String hypothesisStatus) {

        static List<RootCauseCandidate> listFrom(JsonNode array) {
            if (array == null || !array.isArray()) {
                return List.of();
            }
            List<RootCauseCandidate> candidates = new ArrayList<>();
            for (JsonNode node : array) {
                if (!node.isObject()) {
                    throw new IllegalArgumentException("root_cause_candidates entries must be objects");
                }
                candidates.add(new RootCauseCandidate(
                        requiredText(node, "candidate_service"),
                        optionalText(node, "candidate_component"),
                        optionalText(node, "fault_type"),
                        number(node, "rank").asInt(),
                        optionalText(node, "hypothesis_status")));
            }
            return candidates;
        }
    }

    /** 实际消耗：成本统计与实验指标的来源。failed 事件只有 usage_at_failure（无 duration）。 */
    public record Usage(long toolCalls, long tokens, int rounds, int durationSeconds) {

        public static Usage empty() {
            return new Usage(0, 0, 0, 0);
        }

        static Usage fromWire(JsonNode node) {
            if (node == null || !node.isObject()) {
                return empty();
            }
            return new Usage(
                    optionalLong(node, "tool_calls"),
                    optionalLong(node, "tokens"),
                    (int) optionalLong(node, "rounds"),
                    (int) optionalLong(node, "duration_seconds"));
        }
    }

    /** 证据计数摘要（可选字段）。 */
    public record EvidenceSummary(int total, int metrics, int logs, int traces, int events) {

        public static EvidenceSummary empty() {
            return new EvidenceSummary(0, 0, 0, 0, 0);
        }

        static EvidenceSummary fromWire(JsonNode node) {
            if (node == null || !node.isObject()) {
                return empty();
            }
            return new EvidenceSummary(
                    (int) optionalLong(node, "total"),
                    (int) optionalLong(node, "metrics"),
                    (int) optionalLong(node, "logs"),
                    (int) optionalLong(node, "traces"),
                    (int) optionalLong(node, "events"));
        }
    }

    /**
     * 事故 ID 的来源：契约 v1 的 payload 是 {@code additionalProperties:false}，<b>没有</b>
     * {@code incident_id} 字段，事故 ID 走信封的 {@code correlation_id}（contracts/README.md
     * 写明"通常为 incident_id"，agent-runtime 的 {@code _envelope()} 正是这样填的）。
     *
     * <p>同时兼容 payload 里带 {@code incident_id} 的发送方（例如更严格的适配层），
     * 但以契约字段优先、且不做"猜 id"——两边都没有就直接拒收。
     */
    private static String resolveIncidentId(JsonNode envelope, JsonNode payload) {
        JsonNode fromPayload = payload.path("incident_id");
        if (fromPayload.isTextual() && !fromPayload.asText().isBlank()) {
            return fromPayload.asText();
        }
        return requiredText(envelope, "correlation_id");
    }

    private static JsonNode requiredObject(JsonNode parent, String field) {        JsonNode node = parent.path(field);
        if (!node.isObject()) {
            throw new IllegalArgumentException("missing or invalid object field: " + field);
        }
        return node;
    }

    private static String requiredText(JsonNode parent, String field) {
        JsonNode node = parent.path(field);
        if (!node.isTextual() || node.asText().isBlank()) {
            throw new IllegalArgumentException("missing or blank string field: " + field);
        }
        return node.asText();
    }

    private static String optionalText(JsonNode parent, String field) {
        JsonNode node = parent.path(field);
        return node.isTextual() && !node.asText().isBlank() ? node.asText() : null;
    }

    private static boolean requiredBoolean(JsonNode parent, String field) {
        JsonNode node = parent.path(field);
        if (!node.isBoolean()) {
            throw new IllegalArgumentException("missing or invalid boolean field: " + field);
        }
        return node.asBoolean();
    }

    private static JsonNode number(JsonNode parent, String field) {
        JsonNode node = parent.path(field);
        if (!node.isNumber()) {
            throw new IllegalArgumentException("missing or invalid numeric field: " + field);
        }
        return node;
    }

    private static long optionalLong(JsonNode parent, String field) {
        JsonNode node = parent.path(field);
        if (node.isMissingNode() || node.isNull()) {
            return 0L;
        }
        if (!node.isNumber()) {
            throw new IllegalArgumentException("invalid numeric field: " + field);
        }
        return node.asLong();
    }
}
