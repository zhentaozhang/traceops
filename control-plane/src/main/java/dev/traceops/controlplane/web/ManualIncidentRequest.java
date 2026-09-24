package dev.traceops.controlplane.web;

import jakarta.validation.constraints.NotBlank;

/** 人工创建事故的请求体。 */
public record ManualIncidentRequest(
        @NotBlank String service,
        @NotBlank String title,
        String severity,
        /** 为什么手工立这个事故 —— 强制填写，否则事后无从判断是否需要复盘。 */
        @NotBlank String reason) {
}
