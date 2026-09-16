package dev.traceops.controlplane.investigation;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Lob;
import jakarta.persistence.Table;
import java.time.Instant;

/**
 * 一次调查结论的**历史快照**。
 *
 * <p>与 {@link InvestigationFinding} 的区别：那个以 incidentId 为主键、
 * 每个事故只保留最新一条；本表每次调查追加一行，用来回答
 * "这个事故被调查过几次、每次结论有什么变化"（PRD 5.2 的多轮对比）。
 *
 * <p>为什么用追加而不是复用 Finding 表加版本号：
 * 追加表不会让"查最新结论"这条热路径变慢（它仍是一次主键查询），
 * 也不会在删除历史时影响当前结论。
 */
@Entity
@Table(name = "investigation_finding_history")
public class FindingHistorySnapshot {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false)
    private String incidentId;

    @Column(nullable = false)
    private String investigationRunId;

    private String diagnosisStatus;

    private boolean abstained;

    private String rootCauseService;

    private String rootCauseFaultType;

    private int evidenceTotal;

    private int toolCalls;

    private int tokensUsed;

    /** 顺序保留的候选列表（JSON 数组文本） */
    @Lob
    private String rootCauseCandidatesJson;

    @Column(nullable = false)
    private Instant recordedAt;

    protected FindingHistorySnapshot() {
    }

    /** 从当前结论派生一行历史快照。字段按需拷贝，不整对象引用。 */
    public static FindingHistorySnapshot from(InvestigationFinding finding, Instant recordedAt) {
        FindingHistorySnapshot snapshot = new FindingHistorySnapshot();
        snapshot.incidentId = finding.getIncidentId();
        snapshot.investigationRunId = finding.getInvestigationRunId();
        snapshot.diagnosisStatus = finding.getDiagnosisStatus();
        snapshot.abstained = finding.isAbstained();
        snapshot.rootCauseService = finding.getRootCauseService();
        snapshot.rootCauseFaultType = finding.getRootCauseFaultType();
        snapshot.evidenceTotal = finding.getEvidenceTotal();
        snapshot.toolCalls = finding.getToolCalls();
        snapshot.tokensUsed = finding.getTokensUsed();
        snapshot.recordedAt = recordedAt;
        return snapshot;
    }

    public String getInvestigationRunId() {
        return investigationRunId;
    }

    public String getIncidentId() {
        return incidentId;
    }

    public String getDiagnosisStatus() {
        return diagnosisStatus;
    }

    public boolean isAbstained() {
        return abstained;
    }

    public String getRootCauseService() {
        return rootCauseService;
    }

    public String getRootCauseFaultType() {
        return rootCauseFaultType;
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

    public Instant getRecordedAt() {
        return recordedAt;
    }
}
