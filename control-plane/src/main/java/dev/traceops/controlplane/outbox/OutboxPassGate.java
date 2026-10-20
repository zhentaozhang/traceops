package dev.traceops.controlplane.outbox;

import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import org.springframework.stereotype.Component;

/**
 * 投递轮次的**单飞闸门**：同一时刻只允许一轮在跑，并发请求被合并成"再跑一轮"。
 *
 * <p>为什么必须有它：{@link OutboxRelay#relayOnce()} 有三个入口 —— 定时调度、
 * {@code POST /api/v1/diagnostics/outbox/relay} 手工触发，以及测试。定时器本身是单线程的，
 * 所以"两个定时轮次重叠"不会发生；但手工触发不受这个保护：操作员看到积压点一下，
 * 恰好调度轮次正在跑，两轮就会**同时**取到同一批 {@code published_at is null} 的事件。
 *
 * <p>后果不是崩溃，而是更难查的东西：同一批事件被投递两次。消费端按 event_id 幂等
 * 因此不会重复处理 —— 但 outbox 的语义从此变成"可能重复投递"，
 * 而这正是 {@code markPublished} 想避免的。
 *
 * <p>被拒的请求**不能静默丢弃**：操作员点了"现在投递"就必须真的发生一次投递。
 * 所以这里记一个 {@code again} 标记，正在跑的那轮结束后再跑一轮。这与 super-agent
 * 的 pass 合并（{@code src/app/passes.ts:56-62} 的 {@code pumpAgain}）是同一个做法，
 * 只是把它从"调度器内部的实现细节"变成了显式的组件。
 */
@Component
public class OutboxPassGate {

    private final OutboxRelay relay;
    private final AtomicBoolean inFlight = new AtomicBoolean(false);
    private final AtomicBoolean again = new AtomicBoolean(false);
    private final AtomicLong coalesced = new AtomicLong();

    public OutboxPassGate(OutboxRelay relay) {
        this.relay = relay;
    }

    /**
     * 请求跑一轮投递。
     *
     * @return {@code true} 表示本次调用自己跑完了（含合并出的额外轮次）；
     *         {@code false} 表示已有一轮在跑，本次请求被合并
     */
    public boolean requestPass() {
        if (!inFlight.compareAndSet(false, true)) {
            again.set(true);
            coalesced.incrementAndGet();
            return false;
        }
        try {
            do {
                // 必须在跑之前清标记：跑的过程中到达的新请求才会被下一圈看到。
                again.set(false);
                relay.relayOnce();
            } while (again.get());
            return true;
        } finally {
            inFlight.set(false);
        }
    }

    /** 被合并掉的请求数 —— 它对"手工触发有没有真的生效"是唯一证据。 */
    public long coalescedPasses() {
        return coalesced.get();
    }

    /** 是否正在跑一轮（诊断用）。 */
    public boolean inFlight() {
        return inFlight.get();
    }
}
