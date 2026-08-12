package dev.traceops.controlplane.web;

import dev.traceops.controlplane.investigation.InvestigationFinding;
import java.time.Instant;

/**
 * 诊断结论的对外读模型。
 *
 * <p>为什么单独一个 record 而不是直接返回实体：实体带 JPA 注解与懒加载语义，
 * 直接序列化会把持久化细节泄漏到 API 契约里。REST 层应只暴露稳定字段。
 *
 * <p>注意 {@code candidatesJson} 与 {@code propagationChainJson} 是 JSON 文本：
 * 它们来源契约里的可变结构，反序列化成 Java 类型会在契约演进时立刻失效，
 * 保留文本让前端自己解析，契约变更不需要改 Java。
 */
public record FindingResponse(
        String incidentId,
        String investigationRunId,
        String diagnosisStatus,
        boolean abstained,
        boolean needsHumanReview,
        String rootCauseService,
        String rootCauseComponent,
        String rootCauseFaultType,
        String affectedServices,
        String rootCauseCandidates,
        String propagationChain,
        int evidenceTotal,
        int toolCalls,
        int tokensUsed,
        String reportRef,
        Instant updatedAt) {

    public static FindingResponse from(InvestigationFinding finding) {
        return new FindingResponse(
                finding.getIncidentId(),
                finding.getInvestigationRunId(),
                finding.getDiagnosisStatus(),
                finding.isAbstained(),
                finding.isNeedsHumanReview(),
                finding.getRootCauseService(),
                finding.getRootCauseComponent(),
                finding.getRootCauseFaultType(),
                finding.getAffectedServices(),
                finding.getRootCauseCandidatesJson(),
                finding.getPropagationChainJson(),
                finding.getEvidenceTotal(),
                finding.getToolCalls(),
                finding.getTokensUsed(),
                finding.getReportRef(),
                finding.getUpdatedAt());
    }
}
