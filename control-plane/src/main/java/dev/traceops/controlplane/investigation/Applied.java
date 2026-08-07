package dev.traceops.controlplane.investigation;

import dev.traceops.controlplane.domain.IncidentStatus;

/**
 * 消费一条调查终态事件后的应用结果（返回给调用方/消费者）。
 *
 * <p>为什么不是一个薄薄的 boolean：消费端需要区分"已推进"、"重复跳过"、"证据不足需人工"、
 * "状态机拒绝"四种情况——它们对应完全不同的后续动作（ACK、忽略、转人工队列、记录陈旧消息告警）。
 * 用枚举 {@link Outcome} 显式表达，比在调用方猜 {@code succeeded} 的语义安全。
 */
public record Applied(
        String eventId,
        String investigationRunId,
        String incidentId,
        Outcome outcome,
        IncidentStatus previousStatus,
        IncidentStatus resultingStatus,
        InvestigationResult.DiagnosisStatus diagnosisStatus,
        boolean needsHumanReview,
        boolean retryable,
        String reportRef,
        String reason) {

    public enum Outcome {
        /** 状态机被驱动到目标状态（SUPPORTED → DIAGNOSED 或 failure → FAILED）。 */
        APPLIED,
        /** 同一 event_id / run_id 已经处理过：状态机不再被驱动。 */
        SKIPPED_DUPLICATE,
        /** 事件合法但不推进状态（INCONCLUSIVE / REFUTED / 可重试失败），需人工或重跑。 */
        NEEDS_HUMAN_REVIEW,
        /** 可重试的失败：事故保持 INVESTIGATING，等新一轮 Run。 */
        RETRY_PENDING,
        /** 状态机不接受这次转换（如事故已 RESOLVED）：记录原因但不中断消费。 */
        REJECTED,
        /**
         * 消息引用的 incident 不存在（串环境 / 事故已被清理）。
         *
         * <p>选择"返回结果"而不是抛异常：消费循环不能因为一条引用了已删除事故的消息停摆；
         * 异常量应由指标侧观察，而不是用崩溃来表达。异常仍然会在日志里可见。
         */
        NOT_FOUND
    }

    public boolean applied() {
        return outcome == Outcome.APPLIED;
    }
}
