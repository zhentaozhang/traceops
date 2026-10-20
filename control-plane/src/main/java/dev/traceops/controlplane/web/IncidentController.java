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

    public IncidentController(IncidentRepository incidents, IncidentService incidentService,
            IncidentStageService stages) {
        this.incidents = incidents;
        this.incidentService = incidentService;
        this.stages = stages;
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

    @PostMapping("/{id}/transitions")
    public IncidentResponse transition(@PathVariable String id, @Valid @RequestBody TransitionRequest request) {
        Incident incident = incidentService.transition(id, request.target());
        return IncidentResponse.from(incident, stages.describe(incident));
    }
}
