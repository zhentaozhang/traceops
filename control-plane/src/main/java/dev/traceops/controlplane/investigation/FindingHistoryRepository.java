package dev.traceops.controlplane.investigation;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface FindingHistoryRepository extends JpaRepository<FindingHistorySnapshot, Long> {

    List<FindingHistorySnapshot> findByIncidentIdOrderByRecordedAtAsc(String incidentId);
}
