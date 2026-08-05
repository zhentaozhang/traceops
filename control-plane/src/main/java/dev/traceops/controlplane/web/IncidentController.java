package dev.traceops.controlplane.web;

import dev.traceops.controlplane.domain.IncidentRepository;
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

    public IncidentController(IncidentRepository incidents, IncidentService incidentService) {
        this.incidents = incidents;
        this.incidentService = incidentService;
    }

    @GetMapping("/{id}")
    public IncidentResponse get(@PathVariable String id) {
        return incidents.findById(id)
                .map(IncidentResponse::from)
                .orElseThrow(() -> new NoSuchElementException("incident not found: " + id));
    }

    @PostMapping("/{id}/transitions")
    public IncidentResponse transition(@PathVariable String id, @Valid @RequestBody TransitionRequest request) {
        return IncidentResponse.from(incidentService.transition(id, request.target()));
    }
}
