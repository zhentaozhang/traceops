package dev.traceops.controlplane.web;

import dev.traceops.controlplane.service.InvestigationFunnelService;
import java.time.Duration;
import java.time.Instant;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 漏斗读数的只读端点。
 *
 * <p>为什么要端点而不是只打日志：这些数用来回答"要不要加预算"和
 * "结论有没有人看"，两者都需要**跨时间窗**比较，日志里数不出来。
 */
@RestController
@RequestMapping("/api/v1/diagnostics/funnel")
public class FunnelController {

    private final InvestigationFunnelService funnel;

    public FunnelController(InvestigationFunnelService funnel) {
        this.funnel = funnel;
    }

    /**
     * @param since 窗口起点（ISO-8601，可选；默认一小时前）
     */
    @GetMapping
    public InvestigationFunnelService.Funnel snapshot(
            @RequestParam(required = false) String since) {
        Instant windowStart = since == null || since.isBlank()
                ? Instant.now().minus(InvestigationFunnelService.DEFAULT_WINDOW)
                : parse(since);
        return funnel.snapshot(windowStart);
    }

    private static Instant parse(String since) {
        try {
            return Instant.parse(since);
        } catch (java.time.format.DateTimeParseException ex) {
            throw new IllegalArgumentException("since must be an ISO-8601 instant, got: " + since, ex);
        }
    }

    /** 默认窗口，暴露出来给文档和测试对齐。 */
    public static Duration defaultWindow() {
        return InvestigationFunnelService.DEFAULT_WINDOW;
    }
}
