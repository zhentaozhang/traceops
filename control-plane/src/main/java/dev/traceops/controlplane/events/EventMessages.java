package dev.traceops.controlplane.events;

import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/**
 * 按 contracts/ 里的 JSON Schema 组装事件消息：{envelope, payload} 两层。
 *
 * <p>这里刻意手写 Map 而不是注解生成：契约文件是唯一事实来源，
 * 任何字段变动都必须同时改 schema 和 Java 侧的 ContractComplianceTest。
 */
public final class EventMessages {

    public static final String SCHEMA_VERSION = "1.0";
    public static final String PRODUCER = "control-plane";
    public static final String INVESTIGATION_REQUESTED = "investigation.requested";

    /**
     * 进度事件（非终态）。
     *
     * <p>Python 侧每个调查节点发一条，带 {@code progress.step_index}。
     * 它**不是**状态机输入，但也不是"坏消息"。不显式识别的话会被当成
     * 契约违约，每次调查往日志里刷 N 条虚假告警。
     */
    public static final String INVESTIGATION_UPDATED = "investigation.updated";
    public static final Duration DEFAULT_WINDOW = Duration.ofMinutes(5);

    private EventMessages() {
    }

    public static String newEventId() {
        return "evt-" + UUID.randomUUID();
    }

    public static String newRunId() {
        return "run-" + UUID.randomUUID();
    }

    public static Map<String, Object> investigationRequested(
            String incidentId,
            String serviceHint,
            String runId,
            String environment,
            String configVersion,
            String priority,
            Instant now) {
        return investigationRequested(incidentId, serviceHint, runId, environment, configVersion,
                priority, now, now.minus(DEFAULT_WINDOW), now, null);
    }

    /**
     * 组装一条调查请求，允许显式指定时间窗与因果来源。
     *
     * <p>为什么要有这个重载：重试必须问**同一个问题**。
     * {@link #investigationRequested(...)} 永远用"当前时间往前 5 分钟"，
     * 于是每次重试问的窗口都不一样 —— 那不是重试，是换了个问题再问一遍。
     * 重试路径因此必须显式传入原始窗口。
     *
     * <p>{@code causationId} 指向"导致这次重试的那条失败事件"，
     * 缺失时因果链断裂，事后无法回答"这一轮重试是因为哪次失败"。
     */
    public static Map<String, Object> investigationRequested(
            String incidentId,
            String serviceHint,
            String runId,
            String environment,
            String configVersion,
            String priority,
            Instant now,
            Instant windowStart,
            Instant windowEnd,
            String causationId) {

        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("event_id", newEventId());
        envelope.put("event_type", INVESTIGATION_REQUESTED);
        envelope.put("schema_version", SCHEMA_VERSION);
        envelope.put("occurred_at", now.toString());
        envelope.put("correlation_id", incidentId);
        envelope.put("causation_id", causationId);
        envelope.put("aggregate_id", runId);
        envelope.put("aggregate_version", 1);
        envelope.put("producer", PRODUCER);

        Map<String, Object> timeRange = new LinkedHashMap<>();
        timeRange.put("start", windowStart.toString());
        timeRange.put("end", windowEnd.toString());

        Map<String, Object> budget = new LinkedHashMap<>();
        budget.put("max_tool_calls", 50);
        budget.put("max_rounds", 10);
        budget.put("max_tokens", 100_000);
        budget.put("deadline_seconds", 600);

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("investigation_run_id", runId);
        payload.put("incident_id", incidentId);
        // service_hint 是调查起点：缺失时 Agent 无法启动（契约中为必填）
        payload.put("service_hint", serviceHint);
        payload.put("environment", environment);
        payload.put("time_range", timeRange);
        payload.put("config_version", configVersion);
        payload.put("priority", priority);
        payload.put("budget", budget);
        payload.put("scope_depth", 1);

        Map<String, Object> message = new LinkedHashMap<>();
        message.put("envelope", envelope);
        message.put("payload", payload);
        return message;
    }
}
