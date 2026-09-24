package dev.traceops.controlplane.domain;

/** 事故来源。 */
public enum IncidentOrigin {
    /** 告警驱动（webhook）。靠 fingerprint 去重。 */
    ALERT,
    /** 人工创建（SRE 观察或复盘）。**没有 fingerprint，不去重。** */
    MANUAL
}
