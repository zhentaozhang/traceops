package dev.traceops.controlplane.web;

import dev.traceops.controlplane.domain.IncidentStatus;
import java.util.List;

/**
 * 一次 webhook 的处理结果。
 *
 * <p>{@code processed} 是**必须**的：控制器以前只处理 batch 的第一条，
 * 而响应只有一个 incidentId，于是"其余告警被丢弃"这件事在响应里**完全不可见**。
 * 加上计数后，丢弃会立刻暴露。
 */
public record IngestResult(
        String incidentId,
        IncidentStatus status,
        boolean deduplicated,
        int processed,
        int created,
        List<String> incidentIds) {

    /** 单条告警的兼容构造（processed=1, created = 是否新建）。 */
    public IngestResult(String incidentId, IncidentStatus status, boolean deduplicated) {
        this(incidentId, status, deduplicated, 1, deduplicated ? 0 : 1,
                List.of(incidentId == null ? "" : incidentId));
    }
}
