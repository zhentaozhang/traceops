package dev.traceops.controlplane.domain;

/** 非法状态转换：REST 层映射为 409，而不是静默接受。 */
public class IllegalTransitionException extends RuntimeException {

    private final String incidentId;
    private final IncidentStatus from;
    private final IncidentStatus to;

    public IllegalTransitionException(String incidentId, IncidentStatus from, IncidentStatus to) {
        super("illegal transition for incident %s: %s -> %s".formatted(incidentId, from, to));
        this.incidentId = incidentId;
        this.from = from;
        this.to = to;
    }

    public String getIncidentId() {
        return incidentId;
    }

    public IncidentStatus getFrom() {
        return from;
    }

    public IncidentStatus getTo() {
        return to;
    }
}
