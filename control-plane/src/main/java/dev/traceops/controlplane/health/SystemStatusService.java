package dev.traceops.controlplane.health;

import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.investigation.InvestigationProgressStore;
import dev.traceops.controlplane.outbox.OutboxEvent;
import dev.traceops.controlplane.outbox.OutboxRepository;
import jakarta.annotation.PostConstruct;
import java.time.Duration;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import org.springframework.stereotype.Service;

/**
 * 系统状态：把"卡在哪里、卡了多久"变成接口上能看到的东西。
 *
 * <p>存在的理由：这个仓库里所有的失败都是**安静的**。outbox 投递失败只写日志，
 * 重试排了期但没发出去没有任何人知道，进度订阅者被淘汰也没有计数被读。
 * 结果是"服务看起来正常，但所有事故都停在 INVESTIGATING"。
 *
 * <p>和 {@code /health/ready} 的分工：ready 回答"能不能接流量"，
 * 只对编排系统有意义；这里回答"业务流水线此刻健康吗"，是给人看的。
 * 所以它**不参与**摘流量判断 —— 一条积压的 outbox 不该让实例被摘掉。
 *
 * <p>所有条目都必须先在 {@link ComponentHealthRegistry} 里声明归属再上报：
 * 这条约束由注册表强制，本类只是它的第一个使用者。
 */
@Service
public class SystemStatusService {

    public static final String OUTBOX = "outbox.delivery";
    public static final String RETRIES = "investigation.retries";
    public static final String PROGRESS_STORE = "investigation.progress-store";

    /** 归属者。写死一个常量：健康条目的归属必须稳定，否则"谁写的"又会变成猜测。 */
    static final String OWNER = "control-plane";

    /**
     * 积压多久算降级。
     *
     * <p>60 秒是个保守值：relay 每批 50 条、按 5 秒一轮调度，正常情况下
     * 一条事件从写入到发布是秒级。超过一分钟还没出去，说明 relay 或 Redis 有问题，
     * 而不是"刚好赶上一次突发"。
     */
    static final Duration OUTBOX_DEGRADED_AFTER = Duration.ofSeconds(60);

    private final ComponentHealthRegistry registry;
    private final OutboxRepository outbox;
    private final IncidentRepository incidents;
    private final InvestigationProgressStore progressStore;

    public SystemStatusService(
            ComponentHealthRegistry registry,
            OutboxRepository outbox,
            IncidentRepository incidents,
            InvestigationProgressStore progressStore) {
        this.registry = registry;
        this.outbox = outbox;
        this.incidents = incidents;
        this.progressStore = progressStore;
    }

    /**
     * 启动时声明归属。
     *
     * <p>放在构造之后而不是构造里：构造期上报会让"Bean 还没建完"变成一条健康读数，
     * 而那时依赖可能还没就绪。
     */
    @PostConstruct
    void claimEntries() {
        registry.claim(OUTBOX, OWNER);
        registry.claim(RETRIES, OWNER);
        registry.claim(PROGRESS_STORE, OWNER);
    }

    /** 逐条重新计算并上报，然后返回整块快照。 */
    public Map<String, Object> snapshot(Instant now) {
        Instant at = now == null ? Instant.now() : now;
        reportOutbox(at);
        reportRetries(at);
        reportProgressStore(at);

        Map<String, Object> body = new LinkedHashMap<>(registry.snapshot());
        body.put("checkedAt", at.toString());
        // 声明了却从没上报的条目：正常应当为空，非空说明有人加了条目忘了接数据源。
        body.put("claimedButSilent", registry.claimedButSilent());
        return body;
    }

    private void reportOutbox(Instant now) {
        long pending = outbox.countByPublishedAtIsNull();
        Optional<OutboxEvent> oldest = outbox.findFirstByPublishedAtIsNullOrderByCreatedAtAsc();
        if (oldest.isEmpty()) {
            registry.report(OUTBOX, OWNER, ComponentHealthRegistry.Severity.OK,
                    "no pending events", now);
            return;
        }
        Instant since = oldest.get().getCreatedAt();
        long waitedSeconds = since == null ? 0 : Duration.between(since, now).toSeconds();
        ComponentHealthRegistry.Severity severity = ComponentHealthRegistry.Severity.OK;
        if (waitedSeconds >= OUTBOX_DEGRADED_AFTER.toSeconds()) {
            severity = ComponentHealthRegistry.Severity.DEGRADED;
        }
        // 文案里同时给数量和等待时长：只说数量看不出"是不是卡住了"。
        registry.report(OUTBOX, OWNER, severity,
                pending + " pending, oldest waiting " + waitedSeconds + "s since " + since, now);
    }

    private void reportRetries(Instant now) {
        long waiting = incidents.countByNextRetryAtAfter(now);
        long overdue = incidents.countByNextRetryAtBefore(now);
        ComponentHealthRegistry.Severity severity = ComponentHealthRegistry.Severity.OK;
        String detail = waiting + " waiting for backoff";
        if (overdue > 0) {
            // 排期已过却仍挂着 = 排了期但没人发出去。这是重试回路唯一的失败证据。
            severity = ComponentHealthRegistry.Severity.DEGRADED;
            detail = overdue + " retries are due but were never dispatched";
        }
        registry.report(RETRIES, OWNER, severity, detail, now);
    }

    private void reportProgressStore(Instant now) {
        long evictedRuns = progressStore.evictedRuns();
        long droppedSubscribers = progressStore.droppedSubscribers();
        int liveSubscribers = progressStore.totalSubscribers();
        ComponentHealthRegistry.Severity severity = ComponentHealthRegistry.Severity.OK;
        if (droppedSubscribers > 0) {
            // 有订阅者被淘汰 = 有客户端从此收不到进度，而它自己不会知道。
            severity = ComponentHealthRegistry.Severity.DEGRADED;
        }
        registry.report(PROGRESS_STORE, OWNER, severity,
                evictedRuns + " runs evicted, " + droppedSubscribers + " subscribers dropped, "
                        + liveSubscribers + " live subscribers", now);
    }
}
