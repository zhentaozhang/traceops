package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * 进度存储的三道闸：条目 TTL、条目总数上限、单 run 订阅者上限。
 *
 * <p>为什么必须有闸：注释里写着"内存实现是刻意的，因为进度是短生命周期状态"，
 * 但"短生命周期"此前靠 {@code clear(runId)} 保证，而 {@code clear} 由终态事件
 * 触发，终态事件恰恰可能永远不到（worker 崩在 report 节点、消息被丢、
 * 重试耗尽后没人发终态）。于是每个这样的 run 都永久占一条记录。
 *
 * <p>订阅者同理：SSE 断开是客户端行为，浏览器关标签页时服务端未必收到通知。
 * 无界的 map + 无界的 list 在长跑进程里就是内存泄漏，
 * 泄漏速率由事故数量决定 —— 事故越多漏得越快。
 *
 * <p>三道闸都必须<b>可观测</b>：静默丢弃会被误读成"没有发生"。
 */
class InvestigationProgressStoreBoundsTest {

    private static InvestigationProgress progress(String runId, String node, int step) {
        return new InvestigationProgress(runId, node, step, node + " description", step);
    }

    @Test
    void expiredEntriesAreNotServed() throws Exception {
        InvestigationProgressStore store = new InvestigationProgressStore(
                100, Duration.ofMillis(1), 10);
        store.record("run-1", progress("run-1", "collect", 1));
        // 读的时候惰性淘汰，所以必须真的让时间走过去
        Thread.sleep(5);

        assertThat(store.latest("run-1")).isEmpty();
        assertThat(store.evictedRuns()).isEqualTo(1);
    }

    @Test
    void aRepeatedUpdateStillRefreshesTheEntry() {
        // 只收到重复进度（stepIndex 不前进）不该让条目"因为没人看"被淘汰：
        // 前端会突然看不到一个其实还在跑的调查。
        InvestigationProgressStore store = new InvestigationProgressStore(
                100, Duration.ofSeconds(30), 10);
        store.record("run-1", progress("run-1", "collect", 3));
        store.record("run-1", progress("run-1", "collect", 3));
        assertThat(store.latest("run-1")).isPresent();
        assertThat(store.latest("run-1").get().stepIndex()).isEqualTo(3);
    }

    @Test
    void theOldestRunIsEvictedWhenTheCapIsReached() throws Exception {
        InvestigationProgressStore store = new InvestigationProgressStore(2, Duration.ofMinutes(30), 10);
        store.record("run-1", progress("run-1", "plan", 0));
        Thread.sleep(5);
        store.record("run-2", progress("run-2", "plan", 0));
        Thread.sleep(5);
        store.record("run-3", progress("run-3", "plan", 0));

        assertThat(store.count()).isEqualTo(2);
        assertThat(store.latest("run-1")).isEmpty();
        assertThat(store.latest("run-2")).isPresent();
        assertThat(store.latest("run-3")).isPresent();
        assertThat(store.evictedRuns()).isEqualTo(1);
    }

    @Test
    void clearingARunAlsoDropsItsSubscribers() {
        // run_id 复用时会串台：run 结束了还留着 listener，
        // 下一次同名 run 的进度会喂给上一批客户端。
        InvestigationProgressStore store = new InvestigationProgressStore();
        List<InvestigationProgress> seen = new ArrayList<>();
        store.subscribe("run-1", seen::add);
        assertThat(store.subscriberCount("run-1")).isEqualTo(1);

        store.clear("run-1");
        store.record("run-1", progress("run-1", "collect", 1));
        assertThat(seen).isEmpty();
    }

    @Test
    void subscribersPerRunAreCappedAndCounted() {
        InvestigationProgressStore store = new InvestigationProgressStore(
                100, Duration.ofMinutes(30), 2);
        List<InvestigationProgress> first = new ArrayList<>();
        store.subscribe("run-1", first::add);
        store.subscribe("run-1", p -> {
        });
        store.subscribe("run-1", p -> {
        });

        assertThat(store.subscriberCount("run-1")).isEqualTo(2);
        assertThat(store.droppedSubscribers()).isEqualTo(1);

        // 最早的那个被淘汰：泄漏的订阅者不能挤掉真正的订阅者
        store.record("run-1", progress("run-1", "collect", 1));
        assertThat(first).isEmpty();
    }

    @Test
    void unsubscribingReleasesTheSlot() {
        InvestigationProgressStore store = new InvestigationProgressStore(
                100, Duration.ofMinutes(30), 1);
        Runnable unsubscribe = store.subscribe("run-1", p -> {
        });
        unsubscribe.run();
        assertThat(store.subscriberCount("run-1")).isZero();
    }

    @Test
    void nonPositiveLimitsAreRejectedAtConstruction() {
        // 用 0 构造等于"什么都存不下"，那是配置错误而不是策略
        assertThatThrownBy(() -> new InvestigationProgressStore(0, Duration.ofMinutes(1), 1))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new InvestigationProgressStore(1, Duration.ofMinutes(1), 0))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void aNullRunIdIsIgnoredRatherThanStored() {
        InvestigationProgressStore store = new InvestigationProgressStore();
        store.record(null, progress("run-1", "plan", 0));
        store.clear(null);
        assertThat(store.count()).isZero();
        assertThat(store.latest(null)).isEmpty();
    }
}
