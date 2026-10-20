package dev.traceops.controlplane.investigation;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentStatus;
import dev.traceops.controlplane.events.EventMessages;
import dev.traceops.controlplane.outbox.OutboxEvent;
import dev.traceops.controlplane.outbox.OutboxRepository;
import java.time.Duration;
import java.time.Instant;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

/**
 * 可重试失败 → 下一轮调查请求。**这是闭环真正合上的那一段。**
 *
 * <p>此前 {@link InvestigationResultService} 遇到可重试失败时返回
 * {@code RETRY_PENDING}，理由写着"事故保持 INVESTIGATING，等新一轮 Run"——
 * 但**没有任何东西会发出那一轮**。失败一次，事故就永远停在 INVESTIGATING，
 * 再也没有第二次尝试。这条重试路径写在代码里、写在文档里、写在状态机注释里，
 * 唯独不产生任何行为。
 *
 * <p>为什么重试放在控制面而不是 Agent 侧：
 * <ul>
 *   <li>Agent 进程可能随时被杀掉，重试计数必须**跨重启**存活；控制面有数据库。</li>
 *   <li>事故状态机在控制面。重试预算耗尽要把事故推成 FAILED，只有控制面做得到。</li>
 *   <li>Agent 侧发起的重试会绕过幂等账本的控制面检查，容易造成重复调查。</li>
 * </ul>
 *
 * <p>四条不可让步的约束：
 * <ol>
 *   <li><b>必须有上限</b>：无限重试会把一次 MCP 抖动变成烧光预算的循环。</li>
 *   <li><b>必须换新的 run_id</b>：同一个 run_id 会被幂等账本当成同一轮直接跳过。</li>
 *   <li><b>必须复用同一个时间窗</b>：重试是同一个问题，不是新问题。</li>
 *   <li><b>必须带 causation_id</b>：否则事后无法回答"这轮重试是因为哪次失败"。</li>
 * </ol>
 *
 * <p><b>退避放在 outbox 的 {@code notBefore} 上</b>，而不是这里：
 * 调度退避只有一处事实来源，才不会出现"这里以为在退避、relay 却已经发出去了"。
 * relay 会跳过还没到时间的行，因此退避中的重试不会堵住后面的事件。
 */
@Service
public class InvestigationRetryService {

    private static final Logger log = LoggerFactory.getLogger(InvestigationRetryService.class);

    /**
     * 最多重试几轮（不含最初那一轮）。
     *
     * <p>3 轮是个刻意的折中：够跨过一次短暂的 MCP/模型抖动，
     * 又不至于把一次持续故障放大成四次全量调查。
     */
    static final int DEFAULT_MAX_ATTEMPTS = 3;

    /**
     * 第一轮重试的退避时长（秒）。后续轮次按尝试次数线性加长。
     *
     * <p>为什么需要退避：失败往往来自下游抖动（MCP 挂了、模型限流）。
     * 立刻重试只是立刻再撞一次同样的墙，还把失败放大成风暴。
     */
    static final int DEFAULT_BASE_BACKOFF_SECONDS = 5;

    private final OutboxRepository outbox;
    private final ObjectMapper mapper;
    private final String environment;
    private final String configVersion;
    private final int maxAttempts;
    private final Duration baseBackoff;

    public InvestigationRetryService(OutboxRepository outbox, ObjectMapper mapper,
            @Value("${traceops.investigation.environment:demo}") String environment,
            @Value("${traceops.investigation.config-version:cfg-local}") String configVersion,
            @Value("${traceops.investigation.retry.max-attempts:" + DEFAULT_MAX_ATTEMPTS + "}") int maxAttempts,
            @Value("${traceops.investigation.retry.base-backoff-seconds:"
                    + DEFAULT_BASE_BACKOFF_SECONDS + "}") long baseBackoffSeconds) {
        this.outbox = outbox;
        this.mapper = mapper;
        this.environment = environment;
        this.configVersion = configVersion;
        this.maxAttempts = maxAttempts;
        this.baseBackoff = Duration.ofSeconds(baseBackoffSeconds);
    }

    /**
     * 安排一轮重试。
     *
     * @param failedEventId 导致本次重试的失败事件 id（写进 causation_id）
     * @return 是否真的安排了重试
     */
    public boolean scheduleRetry(Incident incident, String failedEventId, Instant now) {
        if (incident == null) {
            throw new IllegalArgumentException("incident is required to schedule a retry");
        }
        // 事故已经进了终态：重试没有意义（也不能把 FAILED 拽回 INVESTIGATING，
        // 状态机里根本没有这条边）。
        if (incident.getStatus() == IncidentStatus.FAILED
                || incident.getStatus() == IncidentStatus.RESOLVED) {
            log.info("retry not scheduled: incident is already terminal: incident={} status={}",
                    incident.getId(), incident.getStatus());
            return false;
        }

        // 预算检查必须在计数之前：排不上的一轮不该消耗预算。
        if (budgetExhausted(incident)) {
            log.warn("retry budget exhausted: incident={} attempts={} max={}",
                    incident.getId(), incident.getRetryAttempts(), maxAttempts);
            return false;
        }
        int attempt = incident.recordRetryAttempt();

        Instant windowStart = incident.getInvestigationWindowStart();
        Instant windowEnd = incident.getInvestigationWindowEnd();
        if (windowStart == null || windowEnd == null) {
            // 事故从来没有记录过调查窗口（老数据 / 手工改动）：退回默认窗口，
            // 而不是发一条没有 time_range 的非法消息。
            windowEnd = now;
            windowStart = now.minus(EventMessages.DEFAULT_WINDOW);
        }

        // 旧 run_id 必须在 beginRun 之前取：重试请求要能说出"我在重做哪一轮"，
        // 否则事件流里三次尝试与三次互不相关的调查长得一模一样。
        String previousRunId = incident.getCurrentRunId();
        String runId = EventMessages.newRunId();
        // 事故跟着换到这一轮的 run_id：进度是按 run 存的状态，不换的话重试真的跑起来以后，
        // 接口还在读上一轮的进度，处境判断会一直停在"已死"。
        incident.beginRun(runId);
        Map<String, Object> message = EventMessages.investigationRequested(
                incident.getId(), incident.getService(), runId, environment, configVersion,
                priorityOf(incident), now, windowStart, windowEnd, failedEventId,
                // 重试必须问同一个问题 —— 包括带上同一份告警原文，
                // 否则重试等于换了个更穷的问题再问一遍。
                incident.getTitle(),
                new EventMessages.RetryContext(previousRunId, incident.getUnavailableSources()));

        String eventId = String.valueOf(((Map<?, ?>) message.get("envelope")).get("event_id"));
        // 退避写进 outbox：这一行在 notBefore 之前不会被 relay 取走。
        Instant notBefore = now.plus(backoffFor(attempt));
        outbox.save(OutboxEvent.pendingAfter(eventId, EventMessages.INVESTIGATION_REQUESTED,
                runId, 1L, toJson(message), now, notBefore));
        // 同时在事故上记下这一轮什么时候会重来：outbox 行的 aggregate_id 是 run_id，
        // 只看 outbox 无法回答"这个事故还要等多久"。调用方随后会保存这个事故。
        incident.scheduleRetryAt(notBefore);

        log.info("retry scheduled: incident={} attempt={}/{} run_id={} retry_of={} known_unavailable={} "
                        + "causation={} not_before={}",
                incident.getId(), attempt, maxAttempts, runId, previousRunId,
                incident.getUnavailableSources(), failedEventId, notBefore);
        return true;
    }

    /**
     * 重试预算是否已经用完。
     *
     * <p>调用方（状态机）据此决定：还能重试就保持 INVESTIGATING，
     * 用完了就必须把事故推成 FAILED —— 否则它会永远停在那里，既不诊断也不失败。
     */
    public boolean budgetExhausted(Incident incident) {
        return incident.getRetryAttempts() >= maxAttempts;
    }

    public int maxAttempts() {
        return maxAttempts;
    }

    /** 第 {@code attempt} 轮重试要等多久：基础退避 × 尝试次数（线性加长）。 */
    Duration backoffFor(int attempt) {
        long multiplier = Math.max(1, attempt);
        return baseBackoff.multipliedBy(multiplier);
    }

    /** 事故的调查优先级：沿用严重级别映射，和告警路径保持一致。 */
    private static String priorityOf(Incident incident) {
        String severity = incident.getSeverity();
        if (severity == null || severity.isBlank()) {
            return "normal";
        }
        return switch (severity.toLowerCase()) {
            case "critical" -> "high";
            case "warning" -> "normal";
            default -> "low";
        };
    }

    private String toJson(Map<String, Object> message) {
        try {
            return mapper.writeValueAsString(message);
        } catch (JsonProcessingException ex) {
            // 序列化失败属于编程错误：事务回滚，绝不落一条发不出去的事件
            throw new IllegalStateException("cannot serialize investigation.requested", ex);
        }
    }
}
