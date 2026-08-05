package dev.traceops.controlplane.web;

import dev.traceops.controlplane.service.IncidentService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/v1/alerts")
public class AlertController {

    private final IncidentService incidentService;

    public AlertController(IncidentService incidentService) {
        this.incidentService = incidentService;
    }

    /** 一次 webhook 取第一条 firing 告警建事故；后续告警由指纹幂等规则处理。 */
    @PostMapping
    public ResponseEntity<IngestResult> ingest(@Valid @RequestBody AlertWebhookRequest request) {
        AlertWebhookRequest.Alert alert = request.alerts().getFirst();
        return ResponseEntity.status(HttpStatus.ACCEPTED).body(incidentService.ingest(alert));
    }
}
