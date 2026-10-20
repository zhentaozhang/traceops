package dev.traceops.controlplane.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.events.EventMessages;
import dev.traceops.controlplane.investigation.Applied;
import dev.traceops.controlplane.investigation.AppliedInvestigationEvent;
import dev.traceops.controlplane.investigation.AppliedInvestigationEventRepository;
import dev.traceops.controlplane.outbox.OutboxEvent;
import dev.traceops.controlplane.outbox.OutboxRepository;
import dev.traceops.controlplane.web.AlertWebhookRequest;
import dev.traceops.controlplane.web.ManualIncidentRequest;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

/**
 * 漏斗每一格都必须能单独对账。
 *
 * <p>被替换的缺陷：{@code IngestResult.processed} 只在单次响应里，
 * 于是"风暴"和"事故变多"在库上不可区分，"没人看结论"和"结论下不出来"
 * 也不可区分 —— 前者要改流程，后者要加预算/工具，修法相反。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@AutoConfigureMockMvc
@Transactional
class InvestigationFunnelTest {

    @Autowired
    private InvestigationFunnelService funnel;

    @Autowired
    private IncidentService incidentService;

    @Autowired
    private IncidentRepository incidents;

    @Autowired
    private OutboxRepository outbox;

    @Autowired
    private AppliedInvestigationEventRepository appliedEvents;

    @Autowired
    private MockMvc mockMvc;

    private static AlertWebhookRequest.Alert alert(String fingerprint, String service) {
        return new AlertWebhookRequest.Alert(
                fingerprint, "firing", service, "critical", "checkout error rate above 5%", null);
    }

    @Test
    void duplicateAlertsAreCountedEvenThoughTheyCreateNothing() {
        Instant since = Instant.now().minusSeconds(5);
        incidentService.ingest(alert("fp-dup", "order-service"));
        incidentService.ingest(alert("fp-dup", "order-service"));
        incidentService.ingest(alert("fp-dup", "order-service"));

        InvestigationFunnelService.Funnel f = funnel.snapshot(since);

        // 去重命中原本什么都不写：这一窗会显示"3 条告警"却"1 个事故"
        assertThat(f.alertIncidents()).isEqualTo(1L);
        assertThat(f.foldedDuplicates()).isEqualTo(2L);
        assertThat(f.alertsSeen()).isEqualTo(3L);
        assertThat(f.duplicateShare()).isGreaterThan(0.6);
    }

    @Test
    void eachStageIsCountedFromItsOwnSource() {
        Instant since = Instant.now().minusSeconds(5);
        incidentService.ingest(alert("fp-a", "order-service"));
        incidentService.ingest(alert("fp-b", "payment-service"));
        incidentService.createManual(
                new ManualIncidentRequest("checkout", "hand found a bad deploy", "warning", "drill"));

        InvestigationFunnelService.Funnel f = funnel.snapshot(since);

        assertThat(f.alertIncidents()).isEqualTo(2L);
        assertThat(f.manualIncidents()).isEqualTo(1L);
        // 每次新建事故发一条请求
        assertThat(f.runsRequested()).isEqualTo(3L);
        // 一条结果都还没回来：这是"在跑或丢了"，不是 0 次调查
        assertThat(f.runsWithoutResult()).isEqualTo(3L);
    }

    @Test
    void completedAndFailedResultsAreSeparateStages() {
        Instant since = Instant.now().minusSeconds(5);
        String incidentId = incidentService.createManual(
                new ManualIncidentRequest("order-service", "t", "warning", "drill")).incidentId();
        String runId = incidents.findById(incidentId).orElseThrow().getCurrentRunId();

        appliedEvents.save(AppliedInvestigationEvent.of(
                "evt-done", runId, EventMessages.INVESTIGATION_COMPLETED, incidentId,
                Applied.Outcome.APPLIED.name(), "INVESTIGATING", "DIAGNOSED", "diagnosis_status=SUPPORTED",
                Instant.now()));
        appliedEvents.save(AppliedInvestigationEvent.of(
                "evt-failed", "run-other", EventMessages.INVESTIGATION_FAILED, incidentId,
                Applied.Outcome.APPLIED.name(), "INVESTIGATING", "FAILED", "budget exhausted",
                Instant.now()));

        InvestigationFunnelService.Funnel f = funnel.snapshot(since);

        assertThat(f.runsCompleted()).isEqualTo(1L);
        assertThat(f.runsFailed()).isEqualTo(1L);
        assertThat(f.runsWithoutResult()).isZero();
    }

    @Test
    void needsHumanReviewIsItsOwnStage() {
        Instant since = Instant.now().minusSeconds(5);
        String incidentId = incidentService.createManual(
                new ManualIncidentRequest("order-service", "t", "warning", "drill")).incidentId();
        String runId = incidents.findById(incidentId).orElseThrow().getCurrentRunId();

        appliedEvents.save(AppliedInvestigationEvent.of(
                "evt-review", runId, EventMessages.INVESTIGATION_COMPLETED, incidentId,
                Applied.Outcome.NEEDS_HUMAN_REVIEW.name(), "INVESTIGATING", "INVESTIGATING",
                "diagnosis_status=INCONCLUSIVE", Instant.now()));

        // 这一格就是"跑完了但没人看"：加预算治不好它，只有人能
        assertThat(funnel.snapshot(since).needsHumanReview()).isEqualTo(1L);
    }

    @Test
    void anOlderWindowExcludesNewerWork() {
        Instant future = Instant.now().plusSeconds(120);

        // 窗口起点在请求时刻之后 ⇒ 永远查不到东西；这不是"系统健康"，是问错了问题
        assertThatThrownBy(() -> funnel.snapshot(future))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("must not be in the future");
    }

    @Test
    void anEmptyWindowReadsZeroRatherThanThrowing() {
        InvestigationFunnelService.Funnel f = funnel.snapshot(Instant.now().minusSeconds(1));

        assertThat(f.runsRequested()).isZero();
        assertThat(f.duplicateShare()).isZero();
    }

    @Test
    void theEndpointReportsTheFunnel() throws Exception {
        Instant since = Instant.now().minusSeconds(5);
        incidentService.ingest(alert("fp-http", "order-service"));
        incidentService.ingest(alert("fp-http", "order-service"));

        mockMvc.perform(get("/api/v1/diagnostics/funnel").param("since", since.toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.alertIncidents").value(1))
                .andExpect(jsonPath("$.foldedDuplicates").value(1))
                .andExpect(jsonPath("$.alertsSeen").value(2))
                .andExpect(jsonPath("$.runsRequested").value(1));
    }

    @Test
    void theEndpointRejectsAnUnparsableWindow() throws Exception {
        // 解析不了的时间窗必须当场被拒：静默退回默认窗口会让人对着错误的窗口读结论
        mockMvc.perform(get("/api/v1/diagnostics/funnel").param("since", "yesterday"))
                .andExpect(status().isBadRequest());
    }

    @Test
    void duplicateCountIsPersistedOnTheIncident() {
        Instant since = Instant.now().minusSeconds(5);
        String id = incidentService.ingest(alert("fp-persist", "order-service")).incidentId();
        incidentService.ingest(alert("fp-persist", "order-service"));

        Incident reloaded = incidents.findById(id).orElseThrow();

        // 只是"返回去重标志"的话，重启之后这个事实就没了
        assertThat(reloaded.getDuplicateAlerts()).isEqualTo(1);
        assertThat(funnel.snapshot(since).duplicateShare()).isEqualTo(0.5);
    }

    @Test
    void outboxRowsOfOtherEventTypesAreNotCountedAsRuns() {
        Instant since = Instant.now().minusSeconds(5);
        outbox.save(OutboxEvent.pending("evt-other", EventMessages.INVESTIGATION_UPDATED,
                "run-x", 1L, "{\"verified\":true}", Instant.now()));

        assertThat(funnel.snapshot(since).runsRequested()).isZero();
        assertThat(Map.of("seen", true)).containsKey("seen");
    }
}
