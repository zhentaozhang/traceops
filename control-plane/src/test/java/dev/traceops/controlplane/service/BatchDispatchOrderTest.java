package dev.traceops.controlplane.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.outbox.OutboxEvent;
import dev.traceops.controlplane.outbox.OutboxRepository;
import dev.traceops.controlplane.web.AlertWebhookRequest;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.transaction.annotation.Transactional;

/**
 * 告警风暴里，严重的那条必须先被调查。
 *
 * <h2>这条规则为什么是"闸门"而不是"排序"</h2>
 *
 * <p>一个组只产生一个调查 run，run 通过 outbox 投递：{@code OutboxRelay} 按插入顺序
 * 每批发 50 条，Python worker 串行消费。所以**投递顺序就是处理顺序**。
 *
 * <p>改之前顺序 = 到达顺序。一批里 49 条 warning + 1 条 critical 时，
 * critical 要等前面全部跑完才轮到 —— 而告警风暴的形状恰好就是
 * "大量低严重度告警里混着少数致命告警"。所以这不是体验问题：
 * 致命故障被自己的噪声淹没了。
 *
 * <p>测试直接看 outbox 里的插入顺序，因为那是唯一能决定 worker 先看到谁的东西。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "traceops.investigation.poller-enabled=false",
})
@Transactional
class BatchDispatchOrderTest {

    @Autowired
    private IncidentService service;

    @Autowired
    private IncidentRepository incidents;

    @Autowired
    private OutboxRepository outbox;

    @Autowired
    private ObjectMapper mapper;

    @Test
    void theCriticalAlertIsDispatchedBeforeTheWarningsThatArrivedFirst() {
        long before = outbox.count();
        List<AlertWebhookRequest.Alert> alerts = new ArrayList<>();
        for (int i = 0; i < 3; i++) {
            alerts.add(alert("fp-noise-" + i, "noise-service-" + i, "warning", "slow query " + i));
        }
        alerts.add(alert("fp-critical", "payment-service", "critical", "connection refused"));

        service.ingestBatch(alerts, List.of());

        assertThat(servicesDispatchedSince(before))
                .first()
                .isEqualTo("payment-service");
    }

    @Test
    void aLowSeverityAlertGoesLast() {
        long before = outbox.count();
        List<AlertWebhookRequest.Alert> alerts = List.of(
                alert("fp-info", "info-service", "info", "informational"),
                alert("fp-warn", "warn-service", "warning", "warning"),
                alert("fp-crit", "crit-service", "critical", "critical"));

        service.ingestBatch(alerts, List.of());

        assertThat(servicesDispatchedSince(before))
                .containsExactly("crit-service", "warn-service", "info-service");
    }

    @Test
    void equalSeverityKeepsArrivalOrder() {
        long before = outbox.count();
        List<AlertWebhookRequest.Alert> alerts = List.of(
                alert("fp-a", "service-a", "warning", "a"),
                alert("fp-b", "service-b", "warning", "b"),
                alert("fp-c", "service-c", "warning", "c"));

        service.ingestBatch(alerts, List.of());

        assertThat(servicesDispatchedSince(before))
                .containsExactly("service-a", "service-b", "service-c");
    }

    /** 只取本次新增的 outbox 事件，按插入顺序还原成"被调查的服务名"。 */
    private List<String> servicesDispatchedSince(long before) {
        List<OutboxEvent> all = outbox.findAll();
        List<OutboxEvent> fresh = all.subList((int) Math.min(before, all.size()), all.size());
        List<String> services = new ArrayList<>();
        for (OutboxEvent event : fresh) {
            services.add(serviceOfIncidentIn(event.getPayload()));
        }
        return services;
    }

    /** payload 里带 incident_id；用它反查事故，再看服务名。 */
    private String serviceOfIncidentIn(String payload) {
        try {
            Map<?, ?> root = mapper.readValue(payload, Map.class);
            Map<?, ?> data = (Map<?, ?>) root.get("payload");
            String incidentId = String.valueOf(data.get("incident_id"));
            return incidents.findById(incidentId)
                    .map(Incident::getService)
                    .orElse("<missing:" + incidentId + ">");
        } catch (Exception e) {
            return "<unparseable:" + e.getMessage() + ">";
        }
    }

    private static AlertWebhookRequest.Alert alert(
            String fingerprint, String service, String severity, String summary) {
        return new AlertWebhookRequest.Alert(
                fingerprint, "firing", service, severity, summary, null);
    }
}
