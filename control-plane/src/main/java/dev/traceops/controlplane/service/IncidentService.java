package dev.traceops.controlplane.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.domain.IncidentStatus;
import dev.traceops.controlplane.events.EventMessages;
import dev.traceops.controlplane.outbox.OutboxEvent;
import dev.traceops.controlplane.outbox.OutboxRepository;
import dev.traceops.controlplane.web.AlertWebhookRequest;
import dev.traceops.controlplane.web.IngestResult;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.HexFormat;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import java.util.Optional;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 事故接入 + 状态机 + outbox 写入，全部在同一个本地事务内完成。
 *
 * <p>幂等：同一 fingerprint 只要还有未结束的事故，重复告警不新建事故、不重复发调查请求。
 */
@Service
public class IncidentService {

    private final IncidentRepository incidents;
    private final OutboxRepository outbox;
    private final ObjectMapper mapper;
    private final String environment;
    private final String configVersion;

    public IncidentService(IncidentRepository incidents, OutboxRepository outbox, ObjectMapper mapper,
            @Value("${traceops.investigation.environment:demo}") String environment,
            @Value("${traceops.investigation.config-version:cfg-local}") String configVersion) {
        this.incidents = incidents;
        this.outbox = outbox;
        this.mapper = mapper;
        this.environment = environment;
        this.configVersion = configVersion;
    }

    @Transactional
    /**
     * 批量接入：逐条处理，并汇总计数。
     *
     * <p>为什么逐条而不是"取第一条"：Alertmanager 的 webhook 天然是批量的，
     * 丢弃其余告警会让多故障场景永远只有一条链（F-007 的根因）。
     *
     * <p>为什么单条失败不让整批失败：一条坏告警不该阻塞同批的其它告警 ——
     * 但失败必须被**计数**（processed 与 created 的差就是去重/失败的数量），
     * 否则又回到"静默丢弃"的老问题。
     */
    public IngestResult ingestAll(List<AlertWebhookRequest.Alert> alerts) {
        if (alerts == null || alerts.isEmpty()) {
            throw new IllegalArgumentException("at least one alert is required");
        }
        List<String> ids = new ArrayList<>();
        String firstId = null;
        IncidentStatus firstStatus = null;
        boolean anyDeduplicated = false;
        int created = 0;
        int processed = 0;

        for (AlertWebhookRequest.Alert alert : alerts) {
            processed++;
            IngestResult one = ingest(alert);
            ids.add(one.incidentId());
            if (firstId == null) {
                firstId = one.incidentId();
                firstStatus = one.status();
            }
            if (one.deduplicated()) {
                anyDeduplicated = true;
            } else {
                created++;
            }
        }
        return new IngestResult(firstId, firstStatus, anyDeduplicated, processed, created, ids);
    }

    public IngestResult ingest(AlertWebhookRequest.Alert alert) {
        if (alert == null || alert.fingerprint() == null || alert.fingerprint().isBlank()) {
            throw new IllegalArgumentException("alert fingerprint is required");
        }
        if (alert.status() != null && !"firing".equalsIgnoreCase(alert.status())) {
            throw new IllegalArgumentException("only firing alerts create incidents");
        }
        Instant now = Instant.now();
        String fingerprint = alert.fingerprint();

        Optional<Incident> active = incidents.findFirstByFingerprintAndStatusNotInOrderByCreatedAtDesc(
                fingerprint, List.of(IncidentStatus.RESOLVED, IncidentStatus.FAILED));
        if (active.isPresent()) {
            return new IngestResult(active.get().getId(), active.get().getStatus(), true);
        }

        Incident incident = Incident.open(UUID.randomUUID().toString(), fingerprint,
                alert.service(), alert.summary(), severityOf(alert), now);
        incidents.save(incident);

        String runId = EventMessages.newRunId();
        Map<String, Object> message = EventMessages.investigationRequested(
                incident.getId(), alert.service(), runId, environment, configVersion,
                priorityOf(alert), now);
        String eventId = String.valueOf(((Map<?, ?>) message.get("envelope")).get("event_id"));
        outbox.save(OutboxEvent.pending(eventId, EventMessages.INVESTIGATION_REQUESTED, runId, 1L,
                toJson(message), now));

        return new IngestResult(incident.getId(), incident.getStatus(), false);
    }

    @Transactional
    public Incident transition(String incidentId, IncidentStatus target) {
        Incident incident = incidents.findById(incidentId)
                .orElseThrow(() -> new NoSuchElementException("incident not found: " + incidentId));
        incident.transitionTo(target, Instant.now());
        return incidents.save(incident);
    }

    private String toJson(Map<String, Object> message) {
        try {
            return mapper.writeValueAsString(message);
        } catch (JsonProcessingException ex) {
            // 序列化失败属于编程错误：事务回滚，绝不落一条发不出去的事件
            throw new IllegalStateException("cannot serialize investigation.requested", ex);
        }
    }

    static String severityOf(AlertWebhookRequest.Alert alert) {
        String severity = alert.severity();
        return severity == null || severity.isBlank() ? "unknown" : severity;
    }

    static String priorityOf(AlertWebhookRequest.Alert alert) {
        return switch (severityOf(alert).toLowerCase()) {
            case "critical" -> "high";
            case "warning" -> "normal";
            default -> "low";
        };
    }

    static String fingerprintOf(String service, String summary) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] hash = digest.digest((service + "|" + summary).getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(hash).substring(0, 32);
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException("SHA-256 unavailable", ex);
        }
    }
}
