package dev.traceops.controlplane.investigation;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface AppliedInvestigationEventRepository
        extends JpaRepository<AppliedInvestigationEvent, String> {

    /** 预检：同一 event_id 是否已被应用（主键查询，零成本）。 */
    Optional<AppliedInvestigationEvent> findByInvestigationRunId(String investigationRunId);
}
