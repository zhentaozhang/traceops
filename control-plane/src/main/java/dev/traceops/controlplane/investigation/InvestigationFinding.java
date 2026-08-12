package dev.traceops.controlplane.investigation;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Lob;
import jakarta.persistence.Table;
import java.time.Instant;
import java.util.List;

/**
 * 调查结论的读模型（"这个事故现在诊断成什么"）。
 *
 * <p>为什么需要它：{@link AppliedInvestigationEvent} 是幂等账本——它记录"这条事件
 * 处理过了没有"，但它**不是**事故当前诊断状态的来源。REST 层要展示"根因是什么、
 * 影响哪些服务、证据有多少"，只能靠这张表。
 *
 * <p>为什么不用 Incident 表存的 JSON 列：诊断结论会被反复覆盖（同一事故可能有
 * 多轮 Run），独立表便于按 run 保留历史，也便于将来做"最近 N 次诊断对比"。
 *
 * <p>每个 incident 一行（以 incident_id 为主键）：展示的是"当前结论"，
 * 而不是全部历史。历史保留在 event 账本与 Run 记录里。
 */
@Entity
@Table(name = "investigation_findings")
public class InvestigationFinding {

    @Id
    private String incidentId;

    private String investigationRunId;

    /** SUPPORTED / REFUTED / INCONCLUSIVE */
    private String diagnosisStatus;

    private boolean abstained;

    private boolean needsHumanReview;

    /** 首选根因服务（rank 1），可为空（INCONCLUSIVE 时可能没有候选） */
    private String rootCauseService;

    private String rootCauseComponent;

    private String rootCauseFaultType;

    /** 逗号分隔的受影响服务（展示用；规模小，不值得再开一张表） */
    @Lob
    private String affectedServices;

    /** 顺序保留的候选列表（JSON 数组文本） */
    @Lob
    private String rootCauseCandidatesJson;

    /** 传播链（JSON 数组文本，元素含 service/role/position） */
    @Lob
    private String propagationChainJson;

    private int evidenceTotal;

    private int toolCalls;

    private int tokensUsed;

    private String reportRef;

    private Instant updatedAt;

    protected InvestigationFinding() {
    }

    public static InvestigationFinding from(
            String incidentId,
            InvestigationResult result,
            Instant now) {
        InvestigationFinding finding = new InvestigationFinding();
        finding.incidentId = incidentId;
        finding.investigationRunId = result.investigationRunId();
        finding.diagnosisStatus = result.diagnosisStatus() == null
                ? "INCONCLUSIVE" : result.diagnosisStatus().name();
        finding.abstained = result.abstained();
        // 证据不足 / 被反驳 / 可重试失败都需要人工确认，这与 service 的判定保持一致
        finding.needsHumanReview = result.diagnosisStatus() != InvestigationResult.DiagnosisStatus.SUPPORTED;
        finding.evidenceTotal = result.evidenceSummary().total();
        // 契约里没有 affected_services / propagation_chain 字段（payload additionalProperties:false），
        // 所以这里不存它们——不能为了"展示好看"而从别处推数据填进去。
        finding.affectedServices = null;
        finding.rootCauseCandidatesJson = candidatesToJson(result.rootCauseCandidates());
        finding.propagationChainJson = null;
        finding.toolCalls = (int) result.usage().toolCalls();
        finding.tokensUsed = (int) result.usage().tokens();
        finding.reportRef = result.reportRef();
        finding.updatedAt = now;

        if (!result.rootCauseCandidates().isEmpty()) {
            InvestigationResult.RootCauseCandidate top = result.rootCauseCandidates().getFirst();
            finding.rootCauseService = top.service();
            finding.rootCauseComponent = top.component();
            finding.rootCauseFaultType = top.faultType();
        }
        return finding;
    }

    private static String candidatesToJson(List<InvestigationResult.RootCauseCandidate> candidates) {
        if (candidates == null || candidates.isEmpty()) {
            return "[]";
        }
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < candidates.size(); i++) {
            InvestigationResult.RootCauseCandidate c = candidates.get(i);
            if (i > 0) {
                sb.append(',');
            }
            sb.append("{\"service\":").append(jsonText(c.service()))
              .append(",\"component\":").append(jsonText(c.component()))
              .append(",\"fault_type\":").append(jsonText(c.faultType()))
              .append(",\"rank\":").append(c.rank())
              .append(",\"hypothesis_status\":").append(jsonText(c.hypothesisStatus()))
              .append('}');
        }
        return sb.append(']').toString();
    }

    private static String jsonText(String value) {
        if (value == null) {
            return "null";
        }
        return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    public String getIncidentId() {
        return incidentId;
    }

    public String getInvestigationRunId() {
        return investigationRunId;
    }

    public String getDiagnosisStatus() {
        return diagnosisStatus;
    }

    public boolean isAbstained() {
        return abstained;
    }

    public boolean isNeedsHumanReview() {
        return needsHumanReview;
    }

    public String getRootCauseService() {
        return rootCauseService;
    }

    public String getRootCauseComponent() {
        return rootCauseComponent;
    }

    public String getRootCauseFaultType() {
        return rootCauseFaultType;
    }

    public String getAffectedServices() {
        return affectedServices;
    }

    public String getRootCauseCandidatesJson() {
        return rootCauseCandidatesJson;
    }

    public String getPropagationChainJson() {
        return propagationChainJson;
    }

    public int getEvidenceTotal() {
        return evidenceTotal;
    }

    public int getToolCalls() {
        return toolCalls;
    }

    public int getTokensUsed() {
        return tokensUsed;
    }

    public String getReportRef() {
        return reportRef;
    }

    public Instant getUpdatedAt() {
        return updatedAt;
    }
}
