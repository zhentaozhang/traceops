package dev.traceops.controlplane.domain;

import java.util.Map;
import java.util.Set;

/**
 * Incident 状态机（蓝皮书 §9 Control Plane）。
 *
 * <p>显式白名单而不是"禁止若干转换"：新增状态时必须显式声明可达边，
 * 否则默认不可达。终态（RESOLVED / FAILED）没有出边。
 */
public enum IncidentStatus {
    NEW,
    INVESTIGATING,
    DIAGNOSED,
    AWAITING_APPROVAL,
    REMEDIATING,
    RESOLVED,
    FAILED;

    private static final Map<IncidentStatus, Set<IncidentStatus>> ALLOWED = Map.of(
            NEW, Set.of(INVESTIGATING, FAILED),
            INVESTIGATING, Set.of(DIAGNOSED, FAILED),
            DIAGNOSED, Set.of(AWAITING_APPROVAL, RESOLVED, FAILED),
            AWAITING_APPROVAL, Set.of(REMEDIATING, RESOLVED, FAILED),
            REMEDIATING, Set.of(RESOLVED, FAILED),
            RESOLVED, Set.of(),
            FAILED, Set.of());

    public boolean canTransitionTo(IncidentStatus target) {
        return ALLOWED.getOrDefault(this, Set.of()).contains(target);
    }

    public boolean isTerminal() {
        return this == RESOLVED || this == FAILED;
    }
}
