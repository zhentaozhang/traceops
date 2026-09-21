package dev.traceops.controlplane.web;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import java.time.Instant;
import java.util.List;

/** 告警 webhook 请求体（P0 自有格式，便于直接映射到 Incident）。 */
public record AlertWebhookRequest(
        @NotEmpty @Valid List<Alert> alerts,
        /**
         * 可选的依赖边 [[caller, callee], ...]。
         *
         * <p>为什么在 webhook 里传：关联判据需要拓扑（"order-service 依赖 redis"），
         * 而 control plane 没有服务注册中心。让推送方带上它，
         * 比在 control plane 里猜拓扑诚实 —— 猜错会把无关告警合并。
         *
         * <p>缺省为空：没有拓扑时只按指纹/同服务关联（保守，不会误合）。
         */
        List<List<String>> dependencies) {

    public AlertWebhookRequest {
        dependencies = dependencies == null ? List.of() : List.copyOf(dependencies);
    }

    public AlertWebhookRequest(List<Alert> alerts) {
        this(alerts, List.of());
    }

    public record Alert(
            String fingerprint,
            String status,
            @NotBlank String service,
            String severity,
            @NotBlank String summary,
            Instant startsAt) {
    }
}
