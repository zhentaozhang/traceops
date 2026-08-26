package dev.traceops.controlplane.investigation;

/**
 * 一轮轮询的结果计数。
 *
 * <p>为什么不用一个 boolean 或 int：消费端需要把"读到多少、推进了多少、为什么没推进"分开看——
 * 这些数字直接决定告警规则（{@code errors} 涨 = 生产端违约或契约漂移；
 * {@code notFound} 持续涨 = 事故落库延迟或串环境；{@code needsReview} 涨 = 需要人工队列）。
 * 全部塞进一个"处理了几条"的计数，出问题时只能翻日志。
 *
 * <p>计数语义（一条消息只进一个桶，{@code processed} 是另外单独计的总数）：
 * <ul>
 *   <li>{@code applied}——状态机真的被驱动了（APPLIED）</li>
 *   <li>{@code skipped}——消息被读到但没有驱动状态机：重复事件（SKIPPED_DUPLICATE）、
 *       并发窗口下撞唯一约束（等价于重复）、以及不是回程终态的类型（例如自己发出的
 *       investigation.requested）</li>
 *   <li>{@code needsReview}——证据不足/被反驳/已诊断后又收到可重试失败，需人工</li>
 *   <li>{@code retryPending}——可重试失败，事故保持 INVESTIGATING 等新一轮 Run</li>
 *   <li>{@code rejected}——状态机不接受（陈旧消息），已记账</li>
 *   <li>{@code notFound}——引用了不存在的事故；这类消息<b>不 ACK</b>，留待下轮</li>
 *   <li>{@code errors}——消息级失败（坏 JSON、契约违约、暂时性异常）加上读取失败</li>
 * </ul>
 */
public record PollResult(
        int processed,
        int applied,
        int skipped,
        int needsReview,
        int retryPending,
        int rejected,
        int notFound,
        int errors) {

    public static PollResult empty() {
        return new PollResult(0, 0, 0, 0, 0, 0, 0, 0);
    }

    /** 本轮读取就失败了（Redis 不可用）：一条消息都没处理，记一个 error。 */
    public static PollResult readFailure() {
        return new PollResult(0, 0, 0, 0, 0, 0, 0, 1);
    }

    /** 本轮是否一切正常：没有错误、也没有需要后续人工/重试的消息。 */
    public boolean isClean() {
        return errors == 0 && notFound == 0;
    }
}
