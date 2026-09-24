package dev.traceops.controlplane.domain;

import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface IncidentRepository extends JpaRepository<Incident, String> {

    /** 幂等接入：同一 fingerprint 仍有未结束事故时不新建。 */
    /**
     * 按指纹找仍活跃的事故（告警去重）。
     *
     * <p>**必须排除 MANUAL**：人工创建没有指纹，若不过滤，
     * 一个 fingerprint=null 的人工事故会被当成"已有活跃事故"，
     * 让后续告警全部误判为重复。
     */
    Optional<Incident> findFirstByFingerprintAndOriginAndStatusNotInOrderByCreatedAtDesc(
            String fingerprint, IncidentOrigin origin, List<IncidentStatus> terminalStatuses);

    java.util.List<Incident> findByServiceOrderByCreatedAtDesc(String service);

    java.util.List<Incident> findAllByOrderByCreatedAtDesc();
}
