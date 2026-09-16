package dev.traceops.controlplane.investigation;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.traceops.controlplane.domain.IllegalTransitionException;
import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.domain.IncidentStatus;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 调查回程的消费端：investigation.completed / investigation.failed → Incident 状态机。
 *
 * <p>此前 Control Plane 只发 investigation.requested，闭环回程是断的：Agent 的结论没人消费，
 * 事故永远停在 INVESTIGATING。本类补上那一段。
 *
 * <p>三条设计原则：
 * <ol>
 *   <li><b>业务状态与幂等账本同事务</b>——与 {@code IncidentService} 的"状态 + outbox 同事务"
 *       同理：绝不能出现"状态推进了但没有幂等记录"（重复消息会再推一次），或反过来
 *       "记了账但状态没推进"（消息被吞）。</li>
 *   <li><b>状态机是唯一入口</b>——一律走 {@link Incident#transitionTo}，绝不 setStatus。
 *       多步路径（NEW → INVESTIGATING → DIAGNOSED）先整体预检、再整体应用，避免留下
 *       推进到一半的中间态。</li>
 *   <li><b>消费端不因单条消息崩溃</b>——晚到、重复、陈旧在异步终态事件里是常态。
 *       状态机拒绝时记录原因并返回 {@code REJECTED}，而不是把异常抛给消费循环。
 *       唯一的例外是生产端违约（event_type / producer 不符）：那必须炸出来。</li>
 * </ol>
 */
@Service
public class InvestigationResultService {

    private static final Logger log = LoggerFactory.getLogger(InvestigationResultService.class);

    /** 契约规定回程终态事件的唯一生产者为 agent-runtime。 */
    static final String EXPECTED_PRODUCER = "agent-runtime";

    private final IncidentRepository incidents;
    private final AppliedInvestigationEventRepository appliedEvents;
    private final InvestigationFindingRepository findings;
    private final FindingHistoryRepository history;
    private final ObjectMapper mapper;

    public InvestigationResultService(IncidentRepository incidents,
            AppliedInvestigationEventRepository appliedEvents,
            InvestigationFindingRepository findings,
            FindingHistoryRepository history,
            ObjectMapper mapper) {
        this.incidents = incidents;
        this.appliedEvents = appliedEvents;
        this.findings = findings;
        this.history = history;
        this.mapper = mapper;
    }

    /**
     * 记录/更新该事故的"当前诊断结论"。
     *
     * <p>为什么放在这里而不是 REST 层：结论是与状态机推进同事务产生的，
     * 分开写会出现"事故已 DIAGNOSED 但查不到结论"的中间状态。
     *
     * <p>INCONCLUSIVE 也写结论：证据不足本身是有价值的信息（缺什么、有哪些候选），
     * 不写等于把"为什么没结论"丢掉。是否需要人工由 needsHumanReview 表达。
     */
    private void recordFinding(String incidentId, InvestigationResult result) {
        Instant now = Instant.now();
        InvestigationFinding current = InvestigationFinding.from(incidentId, result, now);
        findings.save(current);
        // 同时追加一行历史：Finding 以 incidentId 为主键，第二次调查会**覆盖**第一次，
        // 于是"这个事故被重查过几次、结论怎么变"就永久丢失了。
        // 历史表是追加写，不影响"查最新结论"的热路径。
        history.save(FindingHistorySnapshot.from(current, now));
    }

    @Transactional
    public Applied apply(String rawJson) {
        if (rawJson == null || rawJson.isBlank()) {
            throw new IllegalArgumentException("investigation result payload is required");
        }
        JsonNode root = parse(rawJson);
        // 先验证身份再解析内容：身份不符的消息不该进入"解析 payload"这一步，
        // 否则一条 investigation.requested 会因为恰好缺 diagnosis_status 而报出误导性的错误。
        verifyIdentity(root);
        InvestigationResult result = InvestigationResult.fromJson(root);

        Instant now = Instant.now();

        // 幂等预检：event_id（主键）或 run_id（唯一约束）任一命中都说明这次调查已结算过。
        // 两次检查是有意的——上游重发时可能重新生成 event_id，但对同一 Run 的状态推进
        // 只允许发生一次。
        Optional<AppliedInvestigationEvent> alreadyApplied = appliedEvents.findById(result.eventId());
        if (alreadyApplied.isEmpty()) {
            alreadyApplied = appliedEvents.findByInvestigationRunId(result.investigationRunId());
        }
        if (alreadyApplied.isPresent()) {
            AppliedInvestigationEvent record = alreadyApplied.get();
            log.debug("investigation event already applied, skipping: event_id={} run_id={}",
                    result.eventId(), result.investigationRunId());
            return duplicateOf(record, result);
        }

        Optional<Incident> incidentOpt = incidents.findById(result.incidentId());
        if (incidentOpt.isEmpty()) {
            log.warn("investigation result references unknown incident: event_id={} incident_id={}",
                    result.eventId(), result.incidentId());
            String reason = "incident not found: " + result.incidentId();
            appliedEvents.save(AppliedInvestigationEvent.of(result.eventId(), result.investigationRunId(),
                    result.eventType(), result.incidentId(), Applied.Outcome.NOT_FOUND.name(),
                    null, null, reason, now));
            return new Applied(result.eventId(), result.investigationRunId(), result.incidentId(),
                    Applied.Outcome.NOT_FOUND, null, null, result.diagnosisStatus(), false,
                    result.retryable(), result.reportRef(), reason);
        }

        Incident incident = incidentOpt.get();
        IncidentStatus previous = incident.getStatus();
        Applied outcome = drive(incident, result, previous, now);

        // 无论推进、待人工、待重跑还是被拒，都要落账：这条事件已被结算，重复到达必须跳过。
        // "被拒"也记账：陈旧消息重复到达时不该反复触发拒绝日志和指标。
        appliedEvents.save(AppliedInvestigationEvent.of(result.eventId(), result.investigationRunId(),
                result.eventType(), result.incidentId(), outcome.outcome().name(),
                previous.name(), incident.getStatus().name(), outcome.reason(), now));

        return outcome;
    }

    /** 按事件类型 + 诊断状态决定状态机走向；账本写入由 {@link #apply} 统一处理。 */
    private Applied drive(Incident incident, InvestigationResult result, IncidentStatus previous, Instant now) {
        if (result.terminalStatus() == InvestigationResult.TerminalStatus.COMPLETED) {
            return driveCompleted(incident, result, previous, now);
        }
        return driveFailed(incident, result, previous, now);
    }

    private Applied driveCompleted(Incident incident, InvestigationResult result,
            IncidentStatus previous, Instant now) {
        if (result.diagnosisStatus() != InvestigationResult.DiagnosisStatus.SUPPORTED) {
            // INCONCLUSIVE / REFUTED 都不等于"诊断完成"：前者是证据不足（合法的交付物，
            // 但不是一个结论），后者是候选全被反驳。把任一种冒充 DIAGNOSED 都会让
            // 下游审批/处置流程基于不存在的确定性去执行恢复动作。
            // 因此保持原状态，只标记需要人工复核——状态机里没有"诊断未完成"这个态，
            // 硬造一个会破坏白名单语义，也不该在别的状态里夹带诊断进度。
            String reason = "diagnosis_status=" + result.diagnosisStatus()
                    + " is not a completed diagnosis; human review required";
            recordFinding(incident.getId(), result);
            log.info("investigation completed without a supported diagnosis: incident={} status={} reason={}",
                    incident.getId(), previous, result.diagnosisStatus());
            return new Applied(result.eventId(), result.investigationRunId(), result.incidentId(),
                    Applied.Outcome.NEEDS_HUMAN_REVIEW, previous, previous, result.diagnosisStatus(),
                    true, false, result.reportRef(), reason);
        }

        // SUPPORTED = 有被支持的首选假设 = 诊断完成。NEW 需要先补 INVESTIGATING：
        // 状态机不允许 NEW → DIAGNOSED（NEW 的边只有 INVESTIGATING / FAILED），
        // 而"调查确实发生过"在状态机上是真实的一步，不为了少一次写库而绕过它。
        List<IncidentStatus> path = previous == IncidentStatus.NEW
                ? List.of(IncidentStatus.INVESTIGATING, IncidentStatus.DIAGNOSED)
                : List.of(IncidentStatus.DIAGNOSED);
        return advanceOrReject(incident, result, previous, path, now);
    }

    private Applied driveFailed(Incident incident, InvestigationResult result,
            IncidentStatus previous, Instant now) {
        if (result.retryable()) {
            // 可重试：事故保持开放，等新一轮 Run。NEW 说明这次 Run 已实际启动过，补上
            // INVESTIGATING；已经在 INVESTIGATING 就原地不动（不做无含义的自我转换）。
            if (previous == IncidentStatus.NEW) {
                Applied advanced = advanceOrReject(incident, result, previous,
                        List.of(IncidentStatus.INVESTIGATING), now);
                return advanced.outcome() == Applied.Outcome.APPLIED
                        ? new Applied(advanced.eventId(), advanced.investigationRunId(),
                                advanced.incidentId(), Applied.Outcome.RETRY_PENDING,
                                advanced.previousStatus(), advanced.resultingStatus(),
                                null, false, true, result.reportRef(),
                                "retryable failure; incident stays INVESTIGATING for a new run")
                        : advanced;
            }
            if (previous == IncidentStatus.INVESTIGATING) {
                return new Applied(result.eventId(), result.investigationRunId(), result.incidentId(),
                        Applied.Outcome.RETRY_PENDING, previous, previous, null,
                        false, true, result.reportRef(),
                        "retryable failure; incident stays INVESTIGATING for a new run");
            }
            // 已进入诊断/审批/处置阶段却收到可重试失败：状态机没有回退边，
            // 不能把一个已经诊断过的事故拽回 INVESTIGATING，交人工决定是否重开。
            return new Applied(result.eventId(), result.investigationRunId(), result.incidentId(),
                    Applied.Outcome.NEEDS_HUMAN_REVIEW, previous, previous, null,
                    true, true, result.reportRef(),
                    "retryable failure but incident is already " + previous
                            + "; state machine cannot rewind, human decision required");
        }

        // 不可重试：明确失败，转终态 FAILED（需人工介入，不进自动重试）。
        return advanceOrReject(incident, result, previous, List.of(IncidentStatus.FAILED), now);
    }

    /**
     * 预检整条路径再逐跳应用：多步转换中途若非法，不能留下"已经推进了一半"的中间态。
     * 非法转换按约定被捕获并转为 {@code REJECTED}，不打断消费循环。
     */
    private Applied advanceOrReject(Incident incident, InvestigationResult result,
            IncidentStatus previous, List<IncidentStatus> path, Instant now) {
        IncidentStatus current = previous;
        for (IncidentStatus step : path) {
            if (!current.canTransitionTo(step)) {
                IllegalTransitionException illegal =
                        new IllegalTransitionException(incident.getId(), current, step);
                log.info("stale investigation result rejected: {} (event_id={})",
                        illegal.getMessage(), result.eventId());
                return new Applied(result.eventId(), result.investigationRunId(), result.incidentId(),
                        Applied.Outcome.REJECTED, previous, previous, result.diagnosisStatus(),
                        false, result.retryable(), result.reportRef(), illegal.getMessage());
            }
            current = step;
        }

        for (IncidentStatus step : path) {
            try {
                incident.transitionTo(step, now);
            } catch (IllegalTransitionException ex) {
                // 预检与真正应用之间存在理论上的竞态窗口（另一事务已推进了状态）。
                // 兜底同样转成 REJECTED：消费端不能因一条陈旧消息抛出未捕获异常。
                log.info("stale investigation result rejected during apply: {} (event_id={})",
                        ex.getMessage(), result.eventId());
                return new Applied(result.eventId(), result.investigationRunId(), result.incidentId(),
                        Applied.Outcome.REJECTED, previous, previous, result.diagnosisStatus(),
                        false, result.retryable(), result.reportRef(), ex.getMessage());
            }
        }
        incidents.save(incident);
        // 结论与状态机推进同事务写入：避免出现"已 DIAGNOSED 但查不到结论"
        recordFinding(incident.getId(), result);
        return new Applied(result.eventId(), result.investigationRunId(), result.incidentId(),
                Applied.Outcome.APPLIED, previous, incident.getStatus(), result.diagnosisStatus(),
                false, result.retryable(), result.reportRef(), null);
    }

    /**
     * 身份校验：契约文件是唯一事实来源，producer / event_type 不符说明生产端违约、
     * 或有别的东西往回程通道灌消息。必须抛出（进死信、告警），不许"尽力处理"。
     */
    private void verifyIdentity(JsonNode root) {
        JsonNode envelope = root.path("envelope");
        if (!envelope.isObject()) {
            throw new IllegalArgumentException("event envelope is missing");
        }
        String eventType = envelope.path("event_type").asText();
        if (!InvestigationResult.TerminalStatus.isTerminalEventType(eventType)) {
            throw new IllegalArgumentException("unsupported event_type: " + eventType);
        }
        String producer = envelope.path("producer").asText();
        if (!EXPECTED_PRODUCER.equals(producer)) {
            throw new IllegalArgumentException(
                    "unexpected producer for " + eventType + ": " + producer);
        }
    }

    private JsonNode parse(String rawJson) {
        try {
            return mapper.readTree(rawJson);
        } catch (Exception ex) {
            throw new IllegalArgumentException("investigation result is not valid JSON", ex);
        }
    }

    /** 重复事件返回首次结算时记录的结论，避免消费端看到前后不一致的信号。 */
    private static Applied duplicateOf(AppliedInvestigationEvent record, InvestigationResult result) {
        Applied.Outcome recorded = Applied.Outcome.valueOf(record.getOutcome());
        return new Applied(result.eventId(), result.investigationRunId(), record.getIncidentId(),
                Applied.Outcome.SKIPPED_DUPLICATE,
                statusOrNull(record.getPreviousStatus()), statusOrNull(record.getResultingStatus()),
                result.diagnosisStatus(),
                recorded == Applied.Outcome.NEEDS_HUMAN_REVIEW,
                recorded == Applied.Outcome.RETRY_PENDING,
                result.reportRef(),
                "duplicate of applied event " + record.getEventId());
    }

    private static IncidentStatus statusOrNull(String value) {
        return value == null || value.isBlank() ? null : IncidentStatus.valueOf(value);
    }
}
