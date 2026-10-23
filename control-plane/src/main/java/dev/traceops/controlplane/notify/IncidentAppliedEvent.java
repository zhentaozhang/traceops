package dev.traceops.controlplane.notify;

import java.time.Duration;
import java.time.Instant;

/**
 * "这次调查结果已经结算到事故上"的进程内事件。
 *
 * <p>为什么用事件而不是在 {@code InvestigationResultService} 里直接发 HTTP：
 * 状态机推进与幂等账本必须在同一个事务里（已有的约束），而网络调用**不能**在事务里 ——
 * 一次 5 秒的 webhook 超时会把数据库事务拖住，且事务回滚后通知已经发出去了。
 * 事件 + {@code AFTER_COMMIT} 让"提交成功"成为通知的前提。
 */
public record IncidentAppliedEvent(
        String incidentId,
        String service,
        String severity,
        String origin,
        String status,
        String outcome,
        String reason,
        String reportRef,
        Instant at) {

    /** 通知的时效上限：超过这个时间才被送出的通知没有意义，宁可丢。 */
    public static final Duration DELIVERY_BUDGET = Duration.ofMinutes(10);
}
