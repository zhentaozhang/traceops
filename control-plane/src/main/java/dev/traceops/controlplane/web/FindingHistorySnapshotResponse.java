package dev.traceops.controlplane.web;

import dev.traceops.controlplane.investigation.FindingHistorySnapshot;
import java.time.Instant;

/** 一条历史结论的 REST 表示。字段与 FindingResponse 对齐，便于对比同一事故的多轮。 */
public record FindingHistorySnapshotResponse(
        String investigationRunId,
        String diagnosisStatus,
        boolean abstained,
        String rootCauseService,
        String rootCauseFaultType,
        int evidenceTotal,
        int toolCalls,
        int tokensUsed,
        Instant recordedAt) {

    public static FindingHistorySnapshotResponse from(FindingHistorySnapshot snapshot) {
        return new FindingHistorySnapshotResponse(
                snapshot.getInvestigationRunId(),
                snapshot.getDiagnosisStatus(),
                snapshot.isAbstained(),
                snapshot.getRootCauseService(),
                snapshot.getRootCauseFaultType(),
                snapshot.getEvidenceTotal(),
                snapshot.getToolCalls(),
                snapshot.getTokensUsed(),
                snapshot.getRecordedAt());
    }
}
