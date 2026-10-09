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
import dev.traceops.controlplane.domain.IncidentOrigin;
import dev.traceops.controlplane.web.ManualIncidentRequest;
import dev.traceops.controlplane.web.ManualIncidentResult;
import java.util.Set;
import java.util.List;
import java.util.Map;
import java.util.NoSuchElementException;
import dev.traceops.controlplane.correlation.AlertCorrelation;
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
    /** 关联时间窗：同服务/依赖服务的告警在这个窗内视为同一次故障。可配置。 */
    private final java.time.Duration correlationWindow;

    public IncidentService(IncidentRepository incidents, OutboxRepository outbox, ObjectMapper mapper,
            @Value("${traceops.investigation.environment:demo}") String environment,
            @Value("${traceops.investigation.config-version:cfg-local}") String configVersion,
            @Value("${traceops.correlation.window-seconds:600}") long correlationWindowSeconds) {
        this.incidents = incidents;
        this.outbox = outbox;
        this.mapper = mapper;
        this.environment = environment;
        this.configVersion = configVersion;
        this.correlationWindow = java.time.Duration.ofSeconds(correlationWindowSeconds);
    }

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
    /**
     * 批量接入**并按关联关系分组**。
     *
     * <p>为什么必须分组：同一批 webhook 里的多条告警往往描述**同一次故障**
     * （同服务的错误率 + 延迟、依赖方与被依赖方同时报警）。
     * 一条一条独立建事故会让一次故障裂成 N 个事故，值班同学要自己再拼回去 ——
     * 而 `AlertCorrelation` 早就实现了这个判定，却从未被生产代码调用。
     *
     * <p>分组是**传递的**（union-find 式）：A 与 B 相关、B 与 C 相关 ⇒ A/B/C 同组。
     *
     * @param dependencies 可选的依赖边。缺省为空时只按指纹/同服务关联，
     *                     宁可少合并也不要把无关告警揉在一起
     */
    /**
     * 人工创建事故并立即启动一次调查。
     *
     * <p>为什么和告警路径分开而不是复用 {@code ingest}：两者的关键差异是
     * 指纹与去重语义 —— 人工创建是**主动行为**，不该被指纹规则吞掉。
     * 合在一个方法里会迫使每处调用都判断"这次要不要去重"，容易漏。
     */
    @Transactional
    public ManualIncidentResult createManual(ManualIncidentRequest request) {
        Instant now = Instant.now();
        String id = UUID.randomUUID().toString();
        Incident incident = Incident.openManual(
                id, request.service(), request.title(),
                request.severity() == null || request.severity().isBlank()
                        ? "warning" : request.severity(), now);
        incidents.save(incident);

        String runId = EventMessages.newRunId();
        // 记住这次调查要问的窗口：重试必须问同一个问题，否则每轮重试都在换问题。
        Instant windowEnd = now;
        Instant windowStart = now.minus(EventMessages.DEFAULT_WINDOW);
        incident.rememberInvestigationWindow(windowStart, windowEnd);
        Map<String, Object> message = EventMessages.investigationRequested(
                incident.getId(), request.service(), runId, environment, configVersion,
                // 优先级必须是契约枚举里的值（low|normal|high）。此前这里写的是 "manual"，
                // 契约校验会直接判它违规 —— 人工来源由 incident.origin 表达，不该塞进 priority。
                "normal", now, windowStart, windowEnd, null);
        outbox.save(OutboxEvent.pending(EventMessages.newEventId(),
                EventMessages.INVESTIGATION_REQUESTED, runId, 1L, toJson(message), now));
        incident.transitionTo(IncidentStatus.INVESTIGATING, now);
        incidents.save(incident);
        return new ManualIncidentResult(incident.getId(), incident.getStatus(),
                IncidentOrigin.MANUAL, runId);
    }

    @Transactional
    public IngestResult ingestBatch(List<AlertWebhookRequest.Alert> alerts,
                                    List<List<String>> dependencies) {
        if (alerts == null || alerts.isEmpty()) {
            throw new IllegalArgumentException("at least one alert is required");
        }
        AlertCorrelation correlation = new AlertCorrelation(
                correlationWindow,
                AlertCorrelation.staticTopology(edgesOf(dependencies)));

        List<AlertCorrelation.Candidate> candidates = alerts.stream()
                .map(a -> new AlertCorrelation.Candidate(a.fingerprint(), environment,
                        a.service(), a.startsAt() == null ? Instant.now() : a.startsAt()))
                .toList();

        // group() 返回的是按位置分好的组；把每组的第一条作为代表去 ingest，
        // 其余成员与它共享同一事故
        List<List<AlertCorrelation.Candidate>> groups = correlation.group(candidates);
        List<String> ids = new ArrayList<>();
        String firstId = null;
        IncidentStatus firstStatus = null;
        boolean anyDedup = false;
        int created = 0;
        int processed = 0;

        for (List<AlertCorrelation.Candidate> group : groups) {
            IngestResult representative = null;
            for (AlertCorrelation.Candidate member : group) {
                AlertWebhookRequest.Alert alert = findAlert(alerts, member);
                if (alert == null) {
                    continue;
                }
                processed++;
                if (representative == null) {
                    representative = ingest(alert);
                    if (!representative.deduplicated()) {
                        created++;
                    } else {
                        anyDedup = true;
                    }
                    if (firstId == null) {
                        firstId = representative.incidentId();
                        firstStatus = representative.status();
                    }
                    ids.add(representative.incidentId());
                } else {
                    // 同组：不新建事故，直接复用代表的事故
                    ids.add(representative.incidentId());
                }
            }
        }

        return new IngestResult(firstId, firstStatus, anyDedup, processed, created, ids);
    }

    private static Set<List<String>> edgesOf(List<List<String>> dependencies) {
        if (dependencies == null) {
            return Set.of();
        }
        return dependencies.stream()
                .filter(e -> e != null && e.size() >= 2)
                .map(e -> List.of(e.get(0), e.get(1)))
                .collect(java.util.stream.Collectors.toSet());
    }

    private static AlertWebhookRequest.Alert findAlert(
            List<AlertWebhookRequest.Alert> alerts, AlertCorrelation.Candidate candidate) {
        for (AlertWebhookRequest.Alert alert : alerts) {
            if (alert.fingerprint() != null
                    && alert.fingerprint().equals(candidate.fingerprint())) {
                return alert;
            }
        }
        return null;
    }

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

    @Transactional
    public IngestResult ingest(AlertWebhookRequest.Alert alert) {
        if (alert == null || alert.fingerprint() == null || alert.fingerprint().isBlank()) {
            throw new IllegalArgumentException("alert fingerprint is required");
        }
        if (alert.status() != null && !"firing".equalsIgnoreCase(alert.status())) {
            throw new IllegalArgumentException("only firing alerts create incidents");
        }
        Instant now = Instant.now();
        String fingerprint = alert.fingerprint();

        // 只匹配 ALERT 来源：人工事故没有指纹，不能参与告警去重
        Optional<Incident> active =
                incidents.findFirstByFingerprintAndOriginAndStatusNotInOrderByCreatedAtDesc(
                        fingerprint, IncidentOrigin.ALERT,
                        List.of(IncidentStatus.RESOLVED, IncidentStatus.FAILED));
        if (active.isPresent()) {
            return new IngestResult(active.get().getId(), active.get().getStatus(), true);
        }

        Incident incident = Incident.open(UUID.randomUUID().toString(), fingerprint,
                alert.service(), alert.summary(), severityOf(alert), now);
        incidents.save(incident);

        String runId = EventMessages.newRunId();
        // 记住这次调查要问的窗口：重试必须问同一个问题，否则每轮重试都在换问题。
        Instant windowEnd = now;
        Instant windowStart = now.minus(EventMessages.DEFAULT_WINDOW);
        incident.rememberInvestigationWindow(windowStart, windowEnd);
        Map<String, Object> message = EventMessages.investigationRequested(
                incident.getId(), alert.service(), runId, environment, configVersion,
                priorityOf(alert), now, windowStart, windowEnd, null);
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
