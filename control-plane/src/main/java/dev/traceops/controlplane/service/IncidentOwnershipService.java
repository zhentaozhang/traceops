package dev.traceops.controlplane.service;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import java.time.Instant;
import java.util.NoSuchElementException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 事故的"人"：指派、认领、确认结论。
 *
 * <h2>为什么这是独立的一层</h2>
 *
 * <p>状态机描述"流程走到哪"，这些操作描述"谁在管它"。把它们塞进
 * {@code IncidentService}（那是告警入口与状态流转的地方）会让两件事耦合：
 * 认领不需要发调查请求，确认不改变状态。
 *
 * <p>写入口仍然只有 {@link IncidentWriter} 一个 —— 这是上一轮审计定下的约束，
 * 新加一层不是它的例外。
 */
@Service
public class IncidentOwnershipService {

    private static final Logger log = LoggerFactory.getLogger(IncidentOwnershipService.class);

    private final IncidentRepository incidents;
    private final IncidentWriter writer;

    public IncidentOwnershipService(IncidentRepository incidents, IncidentWriter writer) {
        this.incidents = incidents;
        this.writer = writer;
    }

    @Transactional
    public Incident assign(String incidentId, String assignee) {
        Incident incident = require(incidentId);
        String previous = incident.getAssignee();
        incident.assign(assignee, Instant.now());
        writer.save(incident);
        if (previous != null && !previous.equals(incident.getAssignee())) {
            // 改派要留痕：事后复盘"为什么没人管"时，需要知道它被转给过谁。
            log.info("incident reassigned: id={} from={} to={}",
                    incidentId, previous, incident.getAssignee());
        }
        return incident;
    }

    @Transactional
    public Incident acknowledge(String incidentId) {
        Incident incident = require(incidentId);
        incident.acknowledge(Instant.now());
        return writer.save(incident);
    }

    /**
     * 确认或拒绝这一轮的结论。
     *
     * <p>确认本身**不推进状态**：接受一个诊断不等于事故已解决（还要有人处置）。
     * 它记录的是"这条结论被验证过"，下游的跨事故记忆靠它区分
     * "我们猜到的"与"人确认过的"。
     */
    @Transactional
    public Incident confirm(String incidentId, String by, boolean accepted) {
        Incident incident = require(incidentId);
        incident.confirm(by, accepted, Instant.now());
        writer.save(incident);
        log.info("diagnosis confirmation recorded: id={} by={} accepted={}",
                incidentId, incident.getConfirmedBy(), accepted);
        return incident;
    }

    private Incident require(String incidentId) {
        return incidents.findById(incidentId)
                .orElseThrow(() -> new NoSuchElementException("incident not found: " + incidentId));
    }
}
