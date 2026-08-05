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
            String runId,
            String environment,
            String configVersion,
            String priority,
            Instant now) {

        Instant end = now;
        Instant start = now.minus(DEFAULT_WINDOW);

        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("event_id", newEventId());
        envelope.put("event_type", INVESTIGATION_REQUESTED);
        envelope.put("schema_version", SCHEMA_VERSION);
        envelope.put("occurred_at", now.toString());
        envelope.put("correlation_id", incidentId);
        envelope.put("causation_id", null);
        envelope.put("aggregate_id", runId);
        envelope.put("aggregate_version", 1);
        envelope.put("producer", PRODUCER);

        Map<String, Object> timeRange = new LinkedHashMap<>();
        timeRange.put("start", start.toString());
        timeRange.put("end", end.toString());

        Map<String, Object> budget = new LinkedHashMap<>();
        budget.put("max_tool_calls", 50);
        budget.put("max_rounds", 10);
        budget.put("max_tokens", 100_000);
        budget.put("deadline_seconds", 600);

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("investigation_run_id", runId);
        payload.put("incident_id", incidentId);
        payload.put("environment", environment);
        payload.put("time_range", timeRange);
        payload.put("config_version", configVersion);
        payload.put("priority", priority);
        payload.put("budget", budget);

        Map<String, Object> message = new LinkedHashMap<>();
        message.put("envelope", envelope);
        message.put("payload", payload);
        return message;
    }
}
