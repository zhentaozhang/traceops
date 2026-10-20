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

    /** 排期在未来的重试数 —— "有几个事故正在等退避"。 */
    long countByNextRetryAtAfter(java.time.Instant now);

    /**
     * 排期已过、但事故仍停在那里的重试数。
     *
     * <p>这个数长期大于 0 说明"排了期但没人发出去"：relay 没跑、
     * 事件被卡在退避分支之后，或者结果事件丢了。它是重试回路唯一的外部证据。
     */
    long countByNextRetryAtBefore(java.time.Instant now);

    /** 时间窗内新建的事故数（漏斗用）。 */
    long countByCreatedAtAfter(java.time.Instant since);

    /** 时间窗内新建的事故数，按来源分（告警 vs 人工）。 */
    long countByCreatedAtAfterAndOrigin(java.time.Instant since, IncidentOrigin origin);

    /**
     * 时间窗内新建的事故所折叠掉的重复告警总数。
     *
     * <p>口径限制（写在这里以免被当成精确值）：计数挂在**事故**上，
     * 所以一条早就存在的事故在本窗口内新收到的重复告警不会出现在这里。
     * 打开窗口（since 早于事故创建时间）时它是准确的。
     */
    @org.springframework.data.jpa.repository.Query(
            "select coalesce(sum(i.duplicateAlerts), 0) from Incident i where i.createdAt > :since")
    long sumDuplicateAlertsCreatedAfter(java.time.Instant since);
}
