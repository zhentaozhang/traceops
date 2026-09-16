package dev.traceops.controlplane.investigation;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Consumer;
import org.springframework.stereotype.Component;

/**
 * 调查进度的内存存储与订阅。
 *
 * <p>为什么需要它：poller 收到 {@code investigation.updated} 后此前只计数并 ACK，
 * **进度内容被丢弃**，于是 SSE 端点没有数据源。
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
 */
@Component
public class InvestigationProgressStore {

    private final Map<String, InvestigationProgress> latest = new ConcurrentHashMap<>();
    private final Map<String, List<Consumer<InvestigationProgress>>> subscribers =
            new ConcurrentHashMap<>();

    /**
     * 记录一条进度。
     *
     * <p>stepIndex 必须单调递增；比当前更旧的更新会被**丢弃**而不是回退视图。
     */
    public void record(String runId, InvestigationProgress progress) {
        if (runId == null || progress == null) {
            return;
        }
        InvestigationProgress previous = latest.get(runId);
        if (previous != null && progress.stepIndex() <= previous.stepIndex()) {
            // 乱序或重复：保持已有视图。相等也算重复（同一节点重投）。
            return;
        }
        latest.put(runId, progress);
        notify(runId, progress);
    }

    public Optional<InvestigationProgress> latest(String runId) {
        return Optional.ofNullable(latest.get(runId));
    }

    /** 终态到达时清理，否则"正在调查"的判断会永远为真。 */
    public void clear(String runId) {
        latest.remove(runId);
    }

    public int count() {
        return latest.size();
    }

    /** 订阅某 run 的后续进度；返回取消订阅的句柄。 */
    public Runnable subscribe(String runId, Consumer<InvestigationProgress> listener) {
        subscribers.computeIfAbsent(runId, key -> new ArrayList<>()).add(listener);
        return () -> {
            List<Consumer<InvestigationProgress>> list = subscribers.get(runId);
            if (list != null) {
                list.remove(listener);
                if (list.isEmpty()) {
                    subscribers.remove(runId);
                }
            }
        };
    }

    private void notify(String runId, InvestigationProgress progress) {
        List<Consumer<InvestigationProgress>> list = subscribers.get(runId);
        if (list == null || list.isEmpty()) {
            return;
        }
        // 复制一份再遍历：监听器回调里可能取消订阅，直接遍历会 ConcurrentModification
        for (Consumer<InvestigationProgress> listener : List.copyOf(list)) {
            try {
                listener.accept(progress);
            } catch (RuntimeException ignored) {
                // 单个订阅者出错不能影响其他订阅者，也不能影响 poller 主流程
            }
        }
    }
}
