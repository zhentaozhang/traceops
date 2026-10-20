package dev.traceops.controlplane.outbox;

import java.util.List;
import java.util.Map;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.context.ApplicationContext;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * Outbox 诊断端点。
 *
 * <p>为什么需要它：投递链路是跨进程的（控制面 → Redis → worker），
 * 一旦断开，从外部只能看到"没有事件"，无法区分是哪一环坏了 ——
 * 是没写 outbox？是 relay 没跑？还是 publisher 用错了实现？
 *
 * <p>这个端点把三个问题一次性答清楚：待投递条数、用的哪个 publisher、relay 手动触发。
 */
@RestController
@RequestMapping("/api/v1/diagnostics/outbox")
public class OutboxDiagnostics {

    private final OutboxRepository repository;
    private final OutboxPassGate gate;
    private final EventPublisher publisher;
    private final ObjectProvider<OutboxRelayScheduler> scheduler;
    private final ApplicationContext context;

    public OutboxDiagnostics(OutboxRepository repository, OutboxPassGate gate,
                             EventPublisher publisher,
                             ObjectProvider<OutboxRelayScheduler> scheduler,
                             ApplicationContext context) {
        this.repository = repository;
        this.gate = gate;
        this.publisher = publisher;
        this.scheduler = scheduler;
        this.context = context;
    }

    @GetMapping
    public Map<String, Object> state() {
        List<OutboxEvent> pending =
                repository.findTop50ByPublishedAtIsNullOrderByCreatedAtAsc();
        return Map.of(
                "publisher", publisher.getClass().getSimpleName(),
                // 关键诊断：scheduler bean 是否存在（不存在则定时器从不触发）
                "schedulerPresent", scheduler.getIfAvailable() != null,
                "totalRows", repository.count(),
                // 诊断：这些 bean 到底在不在容器里
                "beans", Map.of(
                        "RedisConfig", context.containsBean("redisConfig"),
                        "connectionFactory", context.containsBean("redisConnectionFactory"),
                        "stringRedisTemplate", context.containsBean("stringRedisTemplate")),
                "pending", pending.size(),
                // 用 getEventId 而不是 id：event_id 是幂等键，也是排查用的稳定标识
                "pendingEventIds", pending.stream().map(OutboxEvent::getEventId).toList());
    }

    /** 直接验证 Redis 连通性与 XADD 回执 —— 定位"声明成功但没写"的问题。 */
    @GetMapping("/redis-probe")
    public Map<String, Object> redisProbe(org.springframework.data.redis.core.StringRedisTemplate redis) {
        try {
            String ping = redis.execute((org.springframework.data.redis.core.RedisCallback<String>)
                    (org.springframework.data.redis.connection.RedisConnection c) -> c.ping());
            return Map.of("ok", true, "ping", String.valueOf(ping));
        } catch (Exception ex) {
            return Map.of("ok", false, "error", ex.toString());
        }
    }

    /** 直接调用 publisher —— 区分"relay 没跑"与"publisher 没真发出去"。 */
    @PostMapping("/publish-probe")
    public Map<String, Object> publishProbe() {
        try {
            publisher.publish("{\"probe\":true}");
            return Map.of("ok", true, "publisher", publisher.getClass().getSimpleName());
        } catch (Exception ex) {
            return Map.of("ok", false, "error", ex.toString());
        }
    }

    /**
     * 手动触发一次 relay —— 用来区分"没写 outbox"和"relay 没跑"。
     *
     * <p>走单飞闸门：被合并时返回 {@code started=false} 而不是假装跑过。
     * 之前这里直接调 {@code relayOnce()}，于是操作员在调度轮次运行期间点一下，
     * 两轮会同时取到同一批待投递事件并把它们各发一遍。
     */
    @PostMapping("/relay")
    public Map<String, Object> relay() {
        boolean started = gate.requestPass();
        Map<String, Object> body = new java.util.LinkedHashMap<>();
        body.put("published", started ? "pass-completed" : "coalesced-into-running-pass");
        body.put("started", started);
        body.put("coalescedPasses", gate.coalescedPasses());
        body.put("publisher", publisher.getClass().getSimpleName());
        return body;
    }
}
