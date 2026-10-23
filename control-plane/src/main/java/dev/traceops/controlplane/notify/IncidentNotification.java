package dev.traceops.controlplane.notify;

import java.time.Instant;

/**
 * 一条"要送到人手上"的通知。
 *
 * <p>为什么不是一个自由格式的字符串：接收方（聊天机器人、工单系统、值班脚本）需要
 * 能按字段取用。把结论压成一句人话交给 webhook，接收方就没法按服务聚合、按严重度路由。
 *
 * <p>为什么不带整份报告：报告在对象存储里（{@code reportRef}），通知只负责
 * "有事、什么事、去哪看"。把 10KB 的 JSON 塞进聊天窗口的结果是没人读。
 */
public record IncidentNotification(
        String incidentId,
        String service,
        String severity,
        String origin,
        String status,
        String outcome,
        String reason,
        String reportRef,
        Instant at) {

    /** 一行主题，能直接当标题用。 */
    public String subject() {
        String where = service == null || service.isBlank() ? "unknown service" : service;
        String status = this.status == null ? "unknown" : this.status;
        return "[" + status + "] " + where + " (" + incidentId + ")";
    }

    /** 正文：事故是什么、现在是什么状态、为什么停在这里、去哪看细节。 */
    public String body() {
        StringBuilder sb = new StringBuilder();
        sb.append("incident: ").append(incidentId).append('\n');
        sb.append("service: ").append(service == null ? "-" : service).append('\n');
        sb.append("severity: ").append(severity == null ? "-" : severity).append('\n');
        sb.append("origin: ").append(origin == null ? "-" : origin).append('\n');
        sb.append("status: ").append(status == null ? "-" : status).append('\n');
        sb.append("outcome: ").append(outcome == null ? "-" : outcome).append('\n');
        if (reason != null && !reason.isBlank()) {
            sb.append("why it stopped here: ").append(reason).append('\n');
        }
        // reportRef 可能为空（失败事件没有报告）。空指针地写 "report: null" 会让
        // 接收方以为"有报告但取不到"，所以整行省略。
        if (reportRef != null && !reportRef.isBlank()) {
            sb.append("report: ").append(reportRef).append('\n');
        }
        return sb.toString();
    }
}
