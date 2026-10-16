package dev.traceops.controlplane.investigation;

import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;
import org.springframework.stereotype.Component;

/**
 * 调查进度的内存存储与订阅。
 *
 * <p>为什么需要它：poller 收到 {@code investigation.updated} 后此前只计数并 ACK，
 * <b>进度内容被丢弃</b>，于是 SSE 端点没有数据源。
 *
 * <p>三条语义：
 * <ul>
 *   <li><b>覆盖而不是追加</b>：SSE 只关心"现在到哪一步"</li>
 *   <li><b>不倒退</b>：SSE 消息可能乱序到达（重连/多副本），
 *       旧进度不能覆盖新进度，否则前端会看到进度回退</li>
 *   <li><b>终态清理</b>：run 结束时 {@link #clear}，否则"正在调查"永远为真</li>
 * </ul>
 *
 * <p>内存实现是刻意的：进度是短生命周期状态（秒级到分钟级），
 * 不值得进数据库。进程重启后丢失是可接受的——那时也没有正在跑的调查了。
 *
 * <h2>为什么必须有上限</h2>
 *
 * <p>上面那句"内存实现是刻意的"成立的前提是<b>条目会自动消失</b>，
 * 而此前它只在 {@code clear(runId)} 被显式调用时才消失。问题是
 * {@code clear} 由"终态事件到达"触发，而终态事件恰恰可能永远不到：
 * worker 崩在 report 节点、Redis 消息被丢弃、重试次数耗尽后没人发终态。
 * 于是每一个这样的 run 都会在 {@code latest} 里留一条记录，永不回收。
 *
 * <p>订阅者同理：SSE 连接的断开是<b>客户端行为</b>，浏览器关标签页时
 * 服务端未必收到任何通知。一个没被取消的 listener 会一直挂在这个 run 上。
 * 无界的 map + 无界的 list 在长跑进程里就是内存泄漏，
 * 而且泄漏的速率由"事故数量"决定 —— 事故越多漏得越快。
 *
 * <p>所以这里加三道闸：条目 TTL、条目总数上限、单 run 订阅者上限。
 * 都是 <b>可观测的</b>（{@link #droppedSubscribers()} / {@link #evictedRuns()}），
 * 因为静默丢弃会被误读成"没有发生"。
 */
@Component
public class InvestigationProgressStore {

    /** 条目总数上限。事故高峰时超出的按"最久未更新"淘汰。 */
    public static final int DEFAULT_MAX_RUNS = 2000;

    /**
     * 条目存活时间。
     *
     * <p>取 30 分钟：一次调查的 deadline 是 15 分钟（预算默认 900 秒），
     * 留一倍余量。比 deadline 短会出现"还在跑但进度被删"；
     * 比它长太多则退化成内存泄漏。
     */
    public static final Duration DEFAULT_TTL = Duration.ofMinutes(30);

    /** 单 run 的订阅者上限。超出的按"最早订阅"淘汰。 */
    public static final int DEFAULT_MAX_SUBSCRIBERS_PER_RUN = 64;

    private final Map<String, Entry> latest = new ConcurrentHashMap<>();
    private final Map<String, CopyOnWriteArrayList<Consumer<InvestigationProgress>>> subscribers =
            new ConcurrentHashMap<>();
    private final int maxRuns;
    private final Duration ttl;
    private final int maxSubscribersPerRun;
    private final AtomicLong evictedRuns = new AtomicLong();
    private final AtomicLong droppedSubscribers = new AtomicLong();

    public InvestigationProgressStore() {
        this(DEFAULT_MAX_RUNS, DEFAULT_TTL, DEFAULT_MAX_SUBSCRIBERS_PER_RUN);
    }

    /** 供测试把上限压到很小，从而不必真的造 2000 个 run。 */
    public InvestigationProgressStore(int maxRuns, Duration ttl, int maxSubscribersPerRun) {
        if (maxRuns <= 0) {
            throw new IllegalArgumentException("maxRuns must be positive");
        }
        if (maxSubscribersPerRun <= 0) {
            throw new IllegalArgumentException("maxSubscribersPerRun must be positive");
        }
        this.maxRuns = maxRuns;
        this.ttl = ttl;
        this.maxSubscribersPerRun = maxSubscribersPerRun;
    }

    /**
     * 记录一条进度。
     *
     * <p>stepIndex 必须单调递增；比当前更旧的更新会被<b>丢弃</b>而不是回退视图。
     */
    public void record(String runId, InvestigationProgress progress) {
        if (runId == null || progress == null) {
            return;
        }
        Instant now = Instant.now();
        Entry previous = latest.get(runId);
        if (previous != null && progress.stepIndex() <= previous.progress().stepIndex()) {
            // 乱序或重复：保持已有视图。相等也算重复（同一节点重投）。
            // 注意：这里**不算**"过期"，只是不采纳 —— 但仍刷新活跃时间，
            // 否则一个只剩重复消息的 run 会被 TTL 淘汰，前端瞬间看不到它。
            latest.put(runId, new Entry(previous.progress(), now));
            evictIfNeeded(now);
            return;
        }
        latest.put(runId, new Entry(progress, now));
        // 先存再清：容量判定必须把"刚写进去的这一条"算在内，
        // 否则 cap=2 时写入第三条不会触发淘汰，上限形同虚设。
        evictIfNeeded(now);
        notify(runId, progress);
    }

    public Optional<InvestigationProgress> latest(String runId) {
        if (runId == null) {
            return Optional.empty();
        }
        Entry entry = latest.get(runId);
        if (entry == null) {
            return Optional.empty();
        }
        if (isExpired(entry, Instant.now())) {
            // 惰性淘汰：读的时候顺手清掉。没有调度器也能收敛。
            latest.remove(runId, entry);
            evictedRuns.incrementAndGet();
            return Optional.empty();
        }
        return Optional.of(entry.progress());
    }

    /** 终态到达时清理，否则"正在调查"的判断会永远为真。 */
    public void clear(String runId) {
        if (runId == null) {
            return;
        }
        latest.remove(runId);
        // 订阅者也一起清：run 结束了还留着 listener，等于给下一次同名 run
        // 喂过期回调（run_id 复用时会串台）。
        subscribers.remove(runId);
    }

    public int count() {
        return latest.size();
    }

    /** 被淘汰/过期的条目数。为 0 是好事，但必须能看见它不是 0。 */
    public long evictedRuns() {
        return evictedRuns.get();
    }

    /** 因超限被丢弃的订阅者数。同上：静默丢弃会被误读成"没发生"。 */
    public long droppedSubscribers() {
        return droppedSubscribers.get();
    }

    public int subscriberCount(String runId) {
        List<Consumer<InvestigationProgress>> list = subscribers.get(runId);
        return list == null ? 0 : list.size();
    }

    /** 订阅某 run 的后续进度；返回取消订阅的句柄。 */
    public Runnable subscribe(String runId, Consumer<InvestigationProgress> listener) {
        if (runId == null || listener == null) {
            return () -> {
            };
        }
        // CopyOnWriteArrayList 而不是 ArrayList：listener 回调里常常会取消订阅，
        // 直接遍历 ArrayList 会 ConcurrentModificationException。
        CopyOnWriteArrayList<Consumer<InvestigationProgress>> list =
                subscribers.computeIfAbsent(runId, key -> new CopyOnWriteArrayList<>());
        list.add(listener);
        while (list.size() > maxSubscribersPerRun) {
            // 淘汰最早的那个：泄漏的订阅者不能挤掉真正的订阅者。
            Consumer<InvestigationProgress> oldest = list.get(0);
            if (!list.remove(oldest)) {
                break;
            }
            droppedSubscribers.incrementAndGet();
        }
        return () -> {
            List<Consumer<InvestigationProgress>> current = subscribers.get(runId);
            if (current != null) {
                current.remove(listener);
                if (current.isEmpty()) {
                    subscribers.remove(runId);
                }
            }
        };
    }

    private void evictIfNeeded(Instant now) {
        // 先按 TTL 清一遍：大多数泄漏是"终态事件没来"，TTL 正好治它。
        int before = latest.size();
        if (before > 0) {
            for (Map.Entry<String, Entry> entry : latest.entrySet()) {
                if (isExpired(entry.getValue(), now)) {
                    latest.remove(entry.getKey(), entry.getValue());
                }
            }
        }
        int overflow = latest.size() - maxRuns;
        if (overflow <= 0) {
            return;
        }
        // 仍然超限（短时间内涌入大量 run）：淘汰最久未更新的。
        // 线性扫描可接受：只在超限时走，且规模有上限。
        latest.entrySet().stream()
                .sorted((a, b) -> a.getValue().updatedAt().compareTo(b.getValue().updatedAt()))
                .limit(overflow)
                .toList()
                .forEach(entry -> {
                    if (latest.remove(entry.getKey(), entry.getValue())) {
                        evictedRuns.incrementAndGet();
                    }
                });
    }

    private boolean isExpired(Entry entry, Instant now) {
        return entry.updatedAt().plus(ttl).isBefore(now);
    }

    private void notify(String runId, InvestigationProgress progress) {
        List<Consumer<InvestigationProgress>> list = subscribers.get(runId);
        if (list == null || list.isEmpty()) {
            return;
        }
        // 复制一份再遍历：监听器回调里可能取消订阅
        for (Consumer<InvestigationProgress> listener : List.copyOf(list)) {
            try {
                listener.accept(progress);
            } catch (RuntimeException ignored) {
                // 单个订阅者出错不能影响其他订阅者，也不能影响 poller 主流程
            }
        }
    }

    private record Entry(InvestigationProgress progress, Instant updatedAt) {
    }
}
