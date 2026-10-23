package dev.traceops.controlplane.web;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/** 指派负责人。名字必填：空白名字与"没有负责人"完全一样，却看起来像有人。 */
public record AssignRequest(
        @NotBlank(message = "assignee is required")
        @Size(max = 128, message = "assignee must be at most 128 characters")
        String assignee) {
}
