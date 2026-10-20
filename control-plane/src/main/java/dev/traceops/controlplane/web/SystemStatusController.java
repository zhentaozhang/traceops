package dev.traceops.controlplane.web;

import dev.traceops.controlplane.health.SystemStatusService;
import java.time.Instant;
import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 系统状态端点：流水线此刻健康吗。
 *
 * <p>与 {@code /api/v1/health/ready} 的区别要说清楚，否则运维会用错：
 * ready 失败意味着"摘流量"，本端点失败意味着"去看流水线，但别动实例"。
 * 积压、重试逾期、订阅者被淘汰都属于后者 —— 实例本身完全有能力继续服务。
 */
@RestController
@RequestMapping("/api/v1/status")
public class SystemStatusController {

    private final SystemStatusService status;

    public SystemStatusController(SystemStatusService status) {
        this.status = status;
    }

    /** 每次请求现算：健康快照一旦缓存就会在故障期间显示故障前的样子。 */
    @GetMapping
    public Map<String, Object> status() {
        return status.snapshot(Instant.now());
    }
}
