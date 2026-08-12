package dev.traceops.controlplane.investigation;

import org.springframework.data.jpa.repository.JpaRepository;

public interface InvestigationFindingRepository extends JpaRepository<InvestigationFinding, String> {
}
