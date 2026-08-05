package dev.traceops.controlplane.web;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import java.time.Instant;
import java.util.List;

/** 告警 webhook 请求体（P0 自有格式，便于直接映射到 Incident）。 */
public record AlertWebhookRequest(@NotEmpty @Valid List<Alert> alerts) {

    public record Alert(
            String fingerprint,
            String status,
            @NotBlank String service,
            String severity,
            @NotBlank String summary,
            Instant startsAt) {
    }
}
