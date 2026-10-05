package dev.traceops.controlplane.web;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import javax.sql.DataSource;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 健康端点：**存活**与**就绪**分成两个，语义不同。
 *
 * <p>为什么必须分开（部署实践里最常见的错误就是把它们合成一个）：
 *
 * <ul>
 *   <li><b>liveness</b> — "进程还活着吗"。失败 → 编排系统<b>重启</b>容器。
 *       因此它**绝不能**依赖外部系统：数据库或 Redis 挂了不代表这个进程该被杀，
 *       那只会把"依赖故障"升级成"服务反复重启"。</li>
 *   <li><b>readiness</b> — "现在能接流量吗"。失败 → **摘除流量**但不重启。
 *       它可以检查依赖，因为依赖恢复后进程会自己变健康。</li>
 * </ul>
 *
 * <p>合并成一个端点的代价很具体：一次 Redis 抖动会让所有实例被判死并同时重启，
 * 而那正是最不该重启的时候。
 */
@RestController
@RequestMapping("/api/v1/health")
public class HealthController {

    private final DataSource dataSource;
    private final StringRedisTemplate redis;

    public HealthController(DataSource dataSource, StringRedisTemplate redis) {
        this.dataSource = dataSource;
        this.redis = redis;
    }

    /**
     * 存活探针。**不查任何外部依赖。**
     *
     * <p>只要能响应 HTTP 就说明进程没有死锁/没有耗尽线程 —— 这正是 liveness
     * 该关心的事。查依赖会引入虚假重启。
     */
    @GetMapping("/live")
    public ResponseEntity<Map<String, Object>> live() {
        return ResponseEntity.ok(Map.of(
                "status", "UP",
                "component", "control-plane",
                "checkedAt", Instant.now().toString()));
    }

    /** 有界探测 Redis：超时即视为 DOWN，绝不让健康检查无限等待。 */
    private boolean pingRedisWithDeadline(int seconds) {
        java.util.concurrent.atomic.AtomicBoolean up =
                new java.util.concurrent.atomic.AtomicBoolean(false);
        Thread probe = new Thread(() -> {
            try {
                String pong = redis.execute(
                        (org.springframework.data.redis.core.RedisCallback<String>)
                                (org.springframework.data.redis.connection.RedisConnection c) -> c.ping());
                up.set("PONG".equalsIgnoreCase(pong));
            } catch (Exception ignored) {
                up.set(false);
            }
        }, "redis-health-probe");
        probe.setDaemon(true);
        probe.start();
        try {
            probe.join(java.time.Duration.ofSeconds(seconds).toMillis());
        } catch (InterruptedException ex) {
            Thread.currentThread().interrupt();
            return false;
        }
        // 线程仍在跑 = 超时未返回 → 判 DOWN（不是让它继续挂着）
        return probe.isAlive() ? false : up.get();
    }

    /**
     * 就绪探针。检查依赖，但**依赖故障只影响就绪，不影响存活**。
     *
     * <p>返回 503 表示"暂时别给我流量"，而不是"我该被重启"。
     * 每个依赖单独报告，运维才能一眼看出是数据库还是 Redis 的问题。
     */
    @GetMapping("/ready")
    public ResponseEntity<Map<String, Object>> ready() {
        Map<String, Object> checks = new LinkedHashMap<>();
        boolean allUp = true;

        // 数据库：只验证能拿到连接，不执行业务查询
        try {
            try (var connection = dataSource.getConnection()) {
                checks.put("database", connection.isValid(2) ? "UP" : "DOWN");
                allUp &= connection.isValid(2);
            }
        } catch (Exception ex) {
            checks.put("database", "DOWN");
            allUp = false;
        }

        // Redis：PING 是最轻量的连通性验证。
        //
        // **必须有界**：实测 Redis 不可达时，无超时的 ping 会让整个
        // `/ready` 挂住而不是返回 503 —— 健康检查阻塞比明确报 DOWN 更糟，
        // 因为它会让编排系统连"该摘流量"都判断不了。
        // 用独立线程 + 有界 join，即使底层客户端忽略超时也不会拖住探测。
        checks.put("redis", pingRedisWithDeadline(2) ? "UP" : "DOWN");
        allUp &= "UP".equals(checks.get("redis"));

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", allUp ? "UP" : "DOWN");
        body.put("checks", checks);
        body.put("checkedAt", Instant.now().toString());
        return allUp
                ? ResponseEntity.ok(body)
                : ResponseEntity.status(503).body(body);
    }
}
