package dev.traceops.controlplane.domain;

import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface IncidentRepository extends JpaRepository<Incident, String> {

    /** 幂等接入：同一 fingerprint 仍有未结束事故时不新建。 */
    Optional<Incident> findFirstByFingerprintAndStatusNotInOrderByCreatedAtDesc(
            String fingerprint, List<IncidentStatus> terminalStatuses);
}
