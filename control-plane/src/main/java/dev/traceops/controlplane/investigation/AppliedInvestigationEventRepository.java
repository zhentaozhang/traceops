package dev.traceops.controlplane.investigation;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface AppliedInvestigationEventRepository
        extends JpaRepository<AppliedInvestigationEvent, String> {

    /** 预检：同一 event_id 是否已被应用（主键查询，零成本）。 */
    Optional<AppliedInvestigationEvent> findByInvestigationRunId(String investigationRunId);

    /** 时间窗内应用的结果事件数（窗口内跑完了多少轮调查）。 */
    long countByAppliedAtAfter(java.time.Instant since);

    /** 时间窗内应用的结果事件数，按事件类型分（completed / failed）。 */
    long countByAppliedAtAfterAndEventType(java.time.Instant since, String eventType);

    /** 时间窗内落成某个结论分类的次数（APPLIED / NEEDS_HUMAN_REVIEW ...）。 */
    long countByAppliedAtAfterAndOutcome(java.time.Instant since, String outcome);
}
