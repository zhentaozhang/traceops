package dev.traceops.controlplane.web;

import dev.traceops.controlplane.domain.IncidentStatus;
import jakarta.validation.constraints.NotNull;

public record TransitionRequest(@NotNull IncidentStatus target) {
}
