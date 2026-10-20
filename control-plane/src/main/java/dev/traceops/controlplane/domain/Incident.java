package dev.traceops.controlplane.domain;

import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;
import java.time.Instant;

/** 一次告警对应的事故聚合根。状态只能经 {@link #transitionTo} 变更。 */
@Entity
@Table(name = "incidents")
public class Incident {

    @Id
    private String id;

    /**
     * 告警指纹。**人工创建的事故没有指纹**。
     *
     * <p>为什么允许为空而不是塞一个假值：去重逻辑依赖它，
     * 假指纹会让两条独立的人工事故互相"去重"而只留一条。
     */
    private String fingerprint;

    private String service;

    private String title;

    private String severity;

    /**
     * 事故来源：ALERT（告警驱动）/ MANUAL（人工创建）。
     *
     * <p>必须持久化：事后复盘要能区分"这次是告警触发的"还是"SRE 手工立的"，
     * 而且去重查询必须**排除** MANUAL —— 人工创建是主动行为，不该被指纹规则吞掉。
     */
    @Enumerated(EnumType.STRING)
    private IncidentOrigin origin = IncidentOrigin.ALERT;

    @Enumerated(EnumType.STRING)
    private IncidentStatus status;

    private Instant createdAt;

    private Instant updatedAt;

    /**
     * 首次调查请求使用的时间窗。
     *
     * <p>为什么必须记住它：重试要问的是**同一个问题**。如果重试时用"当前时间往前 5 分钟"，
     * 那每一轮重试问的窗口都不同 —— 这不是重试，是换了问题再问一遍，
     * 结论不可比，还可能查不到同一个故障的证据。
     *
     * <p>只写一次：后续重试不得覆盖（覆盖 = 窗口随重试滑动，退化成新问题）。
     */
    private Instant investigationWindowStart;

    private Instant investigationWindowEnd;

    /**
     * 已经安排过多少轮重试。
     *
     * <p>必须持久化：重试次数是跨进程、跨重启的事实。放在内存里的话，
     * 控制面一重启计数就归零，一次持续抖动就能把重试预算无限刷新。
     */
    private int retryAttempts;

    /**
     * 当前这一轮调查的 run_id。
     *
     * <p>为什么事故要记住它：进度是按 run 存的状态（{@code InvestigationProgressStore}），
     * "这个事故此刻有没有人在跑"只有拿着当前 run_id 才问得出来。此前这个关联只活在
     * outbox 的投递载荷里 —— 于是同一个 {@code INVESTIGATING} 既可能是排队、在跑、
     * 在等退避，也可能根本没有下一轮，而事故本身读不出区别。
     */
    private String currentRunId;

    /**
     * 下一轮重试最早什么时候可以发出；{@code null} 表示当前没有排期中的重试。
     *
     * <p>为什么不只依赖 outbox 的 {@code notBefore}：outbox 行的 aggregate_id 是 run_id，
     * "这个事故在等哪一轮、还要等多久"得反查载荷才能答出来。事故自己记一笔，
     * 才能直接回答"在等退避"还是"重试预算已经用光"。
     *
     * <p>清空时机是**新一轮结果到达**，不是新一轮请求发出：在请求排队那段时间里
     * 若不显示排期，事故又会退回那个说不清的 {@code INVESTIGATING}。
     */
    private Instant nextRetryAt;

    /**
     * 上一轮调查够不到的数据源（逗号分隔，已去重、已排序）。
     *
     * <p>为什么必须由事故记住：重试要"先查上次失败的那个源"。这个信息产生于
     * Agent 的 {@code investigation.completed.failed_sources}，而控制面不读
     * {@code rca_report_ref} 指向的存储 —— 不在这里留一份，重试就只能凭空重排预算，
     * 把同一次失败重演一遍。
     */
    private String unavailableSources;

    /**
     * 这条事故吞掉了多少条重复告警。
     *
     * <p>为什么事故要记这个数：去重发生在 {@code ingest} 里，命中去重时
     * 什么都不落库 —— 于是"收到 200 条告警"和"收到 2 条告警"在库里长得一模一样，
     * 漏斗的第一格就是空的。告警风暴的形状正是"少量事故、海量重复"，
     * 所以这一格必须能数出来。
     */
    private int duplicateAlerts;

    @Version
    private long version;

    protected Incident() {
    }

    private Incident(String id, String fingerprint, String service, String title, String severity, Instant now) {
        this.id = id;
        this.fingerprint = fingerprint;
        this.service = service;
        this.title = title;
        this.severity = severity;
        this.status = IncidentStatus.NEW;
        this.createdAt = now;
        this.updatedAt = now;
    }

    public static Incident open(String id, String fingerprint, String service, String title, String severity, Instant now) {
        return new Incident(id, fingerprint, service, title, severity, now);
    }

    /**
     * 人工创建事故。
     *
     * <p>刻意**不生成 fingerprint**：告警去重靠它，人工创建没有告警。
     * 塞一个假指纹会让两条独立的人工事故互相去重。
     */
    public static Incident openManual(String id, String service, String title, String severity, Instant now) {
        Incident incident = new Incident(id, null, service, title, severity, now);
        incident.origin = IncidentOrigin.MANUAL;
        return incident;
    }

    public void transitionTo(IncidentStatus target, Instant now) {
        if (target == null) {
            throw new IllegalArgumentException("target status is required");
        }
        if (!status.canTransitionTo(target)) {
            throw new IllegalTransitionException(id, status, target);
        }
        this.status = target;
        this.updatedAt = now;
    }

    /**
     * 记住本次调查要问的时间窗。
     *
     * <p>只在第一次记录：后续重试沿用同一个窗口，否则每轮重试都在问不同的问题。
     * 幂等：重复调用不会改写已记录的窗口。
     */
    public void rememberInvestigationWindow(Instant start, Instant end) {
        if (investigationWindowStart == null && start != null && end != null) {
            this.investigationWindowStart = start;
            this.investigationWindowEnd = end;
        }
    }

    /** 记录一次重试，返回累计次数。 */
    public int recordRetryAttempt() {
        this.retryAttempts += 1;
        this.updatedAt = Instant.now();
        return this.retryAttempts;
    }

    /**
     * 开始一轮调查，记住这一轮的 run_id。
     *
     * <p>允许覆盖：重试就是换一个新的 run_id（同一个 run_id 会被幂等账本当成同一轮跳过）。
     * 空值直接拒绝 —— 一个没有 run_id 的"正在调查"与"没在调查"无法区分。
     */
    public void beginRun(String runId) {
        if (runId == null || runId.isBlank()) {
            throw new IllegalArgumentException("run id is required to begin a run");
        }
        this.currentRunId = runId;
        this.updatedAt = Instant.now();
    }

    /** 排下一轮重试：记下最早可发出的时刻。 */
    public void scheduleRetryAt(Instant notBefore) {
        if (notBefore == null) {
            throw new IllegalArgumentException("retry time is required");
        }
        this.nextRetryAt = notBefore;
    }

    /**
     * 清掉重试排期。
     *
     * <p>由"新一轮调查结果到达"触发。留着不清的后果很具体：重试已经跑完，
     * 接口上仍然显示"在等退避"，而真正的处境是"跑完了没结论、没人在动"。
     */
    public void clearRetrySchedule() {
        this.nextRetryAt = null;
    }

    public String getId() {
        return id;
    }

    public String getFingerprint() {
        return fingerprint;
    }

    public String getService() {
        return service;
    }

    public String getTitle() {
        return title;
    }

    public String getSeverity() {
        return severity;
    }

    public IncidentOrigin getOrigin() {
        return origin;
    }

    public IncidentStatus getStatus() {
        return status;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public Instant getUpdatedAt() {
        return updatedAt;
    }

    public long getVersion() {
        return version;
    }

    /** 首次调查请求使用的窗口起点（可能为 null：尚未发过调查请求）。 */
    public Instant getInvestigationWindowStart() {
        return investigationWindowStart;
    }

    public Instant getInvestigationWindowEnd() {
        return investigationWindowEnd;
    }

    public int getRetryAttempts() {
        return retryAttempts;
    }

    /** 当前这一轮调查的 run_id；尚未发起过调查时为 {@code null}。 */
    /**
     * 记住本轮够不到的数据源。
     *
     * <p>语义刻意分成三档，调用方必须先判断消息有没有带这个字段：
     * <ul>
     *   <li>{@code null} —— 这条消息没带，调用方**不该调用本方法**（保留旧值）</li>
     *   <li>空列表 —— 带了，而且这一轮全都够得到 ⇒ 清空（否则下一次重试白跑一趟）</li>
     *   <li>非空 —— 覆盖成本轮的值</li>
     * </ul>
     */
    public void rememberUnavailableSources(java.util.List<String> sources) {
        if (sources == null || sources.isEmpty()) {
            this.unavailableSources = null;
            return;
        }
        java.util.TreeSet<String> unique = new java.util.TreeSet<>();
        for (String source : sources) {
            if (source != null && !source.isBlank()) {
                unique.add(source.trim());
            }
        }
        this.unavailableSources = unique.isEmpty() ? null : String.join(",", unique);
    }

    /** 上一轮够不到的数据源（保持记入时的排序，便于对比两次重试）。 */
    /** 记一条被折叠掉的重复告警。调用方负责在同一事务里写库。 */
    public void recordDuplicateAlert() {
        this.duplicateAlerts++;
    }

    public int getDuplicateAlerts() {
        return duplicateAlerts;
    }

    public java.util.List<String> getUnavailableSources() {
        if (unavailableSources == null || unavailableSources.isBlank()) {
            return java.util.List.of();
        }
        return java.util.List.of(unavailableSources.split(","));
    }

    public String getCurrentRunId() {
        return currentRunId;
    }

    /** 下一轮重试最早可发出时刻；没有排期中的重试时为 {@code null}。 */
    public Instant getNextRetryAt() {
        return nextRetryAt;
    }
}
