package dev.traceops.controlplane.web;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

/**
 * 确认一条诊断结论。
 *
 * @param accepted true = 接受（这条结论被验证过），false = 拒绝（结论错了）
 * @param by       按下这个决定的人。必填而不是从认证里取：目前没有身份认证，
 *                 留一个名字至少让事后能追溯"这是谁按的"。
 */
public record ConfirmRequest(
        @NotNull(message = "accepted is required") Boolean accepted,
        @NotBlank(message = "by is required")
        @Size(max = 128, message = "by must be at most 128 characters") String by) {
}
