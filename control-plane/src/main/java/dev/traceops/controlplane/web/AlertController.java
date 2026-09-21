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

    /**
     * 处理 webhook 里的**每一条**告警。
     *
     * <p>旧实现是 {@code request.alerts().getFirst()}：同批其余告警被静默丢弃。
     * Alertmanager 的 webhook 按设计就是批量的（一个 group 多条 alert），
     * 所以那不是边界情况，是主路径 —— 被丢掉的告警既不入库、也不报错，
     * 直接切断了多故障场景的数据来源（EVALUATION 里 F-007 只能看到一条链即此故）。
     */
    @PostMapping
    public ResponseEntity<IngestResult> ingest(@Valid @RequestBody AlertWebhookRequest request) {
        return ResponseEntity.status(HttpStatus.ACCEPTED)
                .body(incidentService.ingestBatch(request.alerts(), request.dependencies()));
    }
}
