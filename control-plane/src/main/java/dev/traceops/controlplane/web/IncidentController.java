package dev.traceops.controlplane.web;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.investigation.IncidentStageService;
import dev.traceops.controlplane.service.IncidentService;
import jakarta.validation.Valid;
import java.util.NoSuchElementException;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/incidents")
public class IncidentController {

    private final IncidentRepository incidents;
    private final IncidentService incidentService;
    private final IncidentStageService stages;
    private final dev.traceops.controlplane.service.IncidentOwnershipService ownership;

    public IncidentController(IncidentRepository incidents, IncidentService incidentService,
            IncidentStageService stages,
            dev.traceops.controlplane.service.IncidentOwnershipService ownership) {
        this.incidents = incidents;
        this.incidentService = incidentService;
        this.stages = stages;
        this.ownership = ownership;
    }

    /**
     * 读取单个事故。
     *
     * <p>返回体除状态机事实外还带一个派生的 {@code stage}：同一个 {@code INVESTIGATING}
     * 可能是排队、在跑、在等退避、或者已经没有下一轮，只有 {@code stage} 能区分。
     */
    @GetMapping("/{id}")
    public IncidentResponse get(@PathVariable String id) {
        Incident incident = incidents.findById(id)
                .orElseThrow(() -> new NoSuchElementException("incident not found: " + id));
        return IncidentResponse.from(incident, stages.describe(incident));
    }

    /**
     * 指派负责人。
     *
     * <p>为什么是 PATCH 语义却用 POST：这条端点改变的是"谁在管"，属于动作而非
     * 资源整体替换；用 POST /assign 让审计日志里的意图一眼可读。
     */
    @PostMapping("/{id}/assign")
    public IncidentResponse assign(@PathVariable String id,
            @Valid @RequestBody AssignRequest request) {
        var incident = ownership.assign(id, request.assignee());
        return IncidentResponse.from(incident, stages.describe(incident));
    }

    /** 有人接了这条事故。重复调用不覆盖首次时间。 */
    @PostMapping("/{id}/ack")
    public IncidentResponse acknowledge(@PathVariable String id) {
        var incident = ownership.acknowledge(id);
        return IncidentResponse.from(incident, stages.describe(incident));
    }

    /**
     * 确认或拒绝这一轮的诊断结论。
     *
     * <p>为什么需要 {@code by} 而不是从认证里取：这套系统目前没有身份认证
     * （见 RUNBOOK「已知缺口」）。留一个必填的名字，比假装有认证诚实 ——
     * 至少事后能问"这是谁按的"。
     */
    @PostMapping("/{id}/confirm")
    public IncidentResponse confirm(@PathVariable String id,
            @Valid @RequestBody ConfirmRequest request) {
        var incident = ownership.confirm(id, request.by(), request.accepted());
        return IncidentResponse.from(incident, stages.describe(incident));
    }

    @PostMapping("/{id}/transitions")
    public IncidentResponse transition(@PathVariable String id, @Valid @RequestBody TransitionRequest request) {
        Incident incident = incidentService.transition(id, request.target());
        return IncidentResponse.from(incident, stages.describe(incident));
    }
}
