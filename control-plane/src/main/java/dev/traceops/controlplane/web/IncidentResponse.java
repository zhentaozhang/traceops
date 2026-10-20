package dev.traceops.controlplane.web;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentOrigin;
import dev.traceops.controlplane.domain.IncidentStage;
import dev.traceops.controlplane.domain.IncidentStatus;
import java.time.Instant;

/**
 * 事故的 REST 表示。
 *
 * @param status 流程走到哪一步（状态机事实）
 * @param stage  此刻卡在谁那里（派生视图）；拿不到信号时为 {@code null}。
 *               两者刻意分开：{@code status} 是持久事实，{@code stage} 是随时间和
 *               最近一条回程事件变化的判断，混成一个字段会让"状态变了"和"处境变了"
 *               无法区分（例如退避到期只是处境变了，状态一直是 INVESTIGATING）。
 */
public record IncidentResponse(
        String id,
        String service,
        String title,
        String severity,
        IncidentStatus status,
        IncidentOrigin origin,
        Instant createdAt,
        IncidentStage stage) {

    public static IncidentResponse from(Incident incident) {
        return from(incident, null);
    }

    public static IncidentResponse from(Incident incident, IncidentStage stage) {
        return new IncidentResponse(incident.getId(), incident.getService(),
                incident.getTitle(), incident.getSeverity(), incident.getStatus(),
                incident.getOrigin(), incident.getCreatedAt(), stage);
    }
}
