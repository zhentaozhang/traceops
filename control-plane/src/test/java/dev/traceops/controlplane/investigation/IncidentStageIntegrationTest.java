package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.domain.IncidentStage;
import dev.traceops.controlplane.domain.IncidentStatus;
import dev.traceops.controlplane.service.IncidentWriter;
import dev.traceops.controlplane.web.ManualIncidentRequest;
import dev.traceops.controlplane.service.IncidentService;
import java.time.Instant;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

/**
 * 处境派生的真实链路测试：事故 → 当前 run → 进度 / 结算结论 / 重试排期 → 处境。
 *
 * <p>为什么必须是集成测试而不是纯单元：这一项改动真正的价值是"把散在三处的信号收拢"，
 * 只测 {@code IncidentStage.derive} 等于只测了一半 —— 三处信号如果根本没接上，
 * 纯函数再对也不会改变任何人的处境。这里不 mock：真实 H2、真实进度存储、真实账本。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@AutoConfigureMockMvc
@Transactional
class IncidentStageIntegrationTest {

    @Autowired
    private IncidentService incidentService;

    @Autowired
    private IncidentRepository incidents;

    @Autowired
    private IncidentWriter writer;

    @Autowired
    private IncidentStageService stages;

    @Autowired
    private InvestigationProgressStore progress;

    @Autowired
    private AppliedInvestigationEventRepository appliedEvents;

    @Autowired
    private InvestigationRetryService retries;

    @Autowired
    private MockMvc mvc;

    private String incidentId;
    private String runId;

    @BeforeEach
    void createAnIncidentWithARun() {
        var result = incidentService.createManual(new ManualIncidentRequest(
                "order-service", "checkout errors after deploy", "warning",
                "synthetic incident for the stage derivation test"));
        incidentId = result.incidentId();
        runId = result.investigationRunId();
    }

    private Incident reload() {
        return incidents.findById(incidentId).orElseThrow();
    }

    /**
     * run_id 必须真的落库：它是"此刻有没有人在跑"的唯一线索，
     * 留在 outbox 载荷里等于没有。
     */
    @Test
    void theIncidentRemembersTheCurrentRun() {
        assertThat(reload().getCurrentRunId()).isEqualTo(runId);
    }

    @Test
    void aClaimedIncidentWithoutProgressIsQueued() {
        IncidentStage stage = stages.describe(reload());

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.QUEUED);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.WORKER);
    }

    @Test
    void progressFromTheCurrentRunMakesItRunning() {
        progress.record(runId, new InvestigationProgress(runId, "collect", 3, "collecting evidence", 5));

        IncidentStage stage = stages.describe(reload());

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.RUNNING);
        assertThat(stage.detail()).contains("step 3").contains("collect");
    }

    /**
     * 这一条是整个改动的靶心：INCONCLUSIVE 之后事故停在 INVESTIGATING，
     * 但从接口上看必须显示"等人决定、且没有自动路径"，而不是"正在调查"。
     */
    @Test
    void anInconclusiveOutcomeTurnsTheStageIntoNeedsDecision() {
        appliedEvents.save(AppliedInvestigationEvent.of("evt-conclusive-less", runId,
                "investigation.completed", incidentId, Applied.Outcome.NEEDS_HUMAN_REVIEW.name(),
                "INVESTIGATING", "INVESTIGATING", "diagnosis_status=INCONCLUSIVE", Instant.now()));

        IncidentStage stage = stages.describe(reload());

        assertThat(reload().getStatus()).as("状态机事实不变").isEqualTo(IncidentStatus.INVESTIGATING);
        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.NEEDS_DECISION);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.PERSON);
        assertThat(stage.retryable()).isFalse();
    }

    /**
     * 重试排期必须落到事故上并且能跨读取存活：这是"还要等多久"的唯一来源。
     */
    @Test
    void aScheduledRetryShowsUpAsBackingOff() {
        Incident incident = reload();
        Instant notBefore = Instant.now().plusSeconds(30);
        incident.beginRun("run-retry-1");
        incident.scheduleRetryAt(notBefore);
        writer.save(incident);

        IncidentStage stage = stages.describe(reload());

        assertThat(reload().getNextRetryAt()).isEqualTo(notBefore);
        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.RETRY_BACKOFF);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.CLOCK);
    }

    /** 重试预算用光之后不能还显示"在等退避"，那是把死状态伪装成活状态。 */
    @Test
    void anExhaustedBudgetIsDeadRatherThanBackingOff() {
        Incident incident = reload();
        for (int i = 0; i < retries.maxAttempts(); i++) {
            incident.recordRetryAttempt();
        }
        incident.clearRetrySchedule();
        appliedEvents.save(AppliedInvestigationEvent.of("evt-retry-pending", runId,
                "investigation.failed", incidentId, Applied.Outcome.RETRY_PENDING.name(),
                "INVESTIGATING", "INVESTIGATING", "retryable failure", Instant.now()));
        writer.save(incident);

        IncidentStage stage = stages.describe(reload());

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.DEAD);
        assertThat(stage.detail()).contains("retry budget exhausted");
    }

    /**
     * 接口上必须看得到：派生视图接不进响应体，等于没人能用它。
     */
    @Test
    void theEndpointReturnsTheDerivedStage() throws Exception {
        progress.record(runId, new InvestigationProgress(runId, "hypothesize", 4, "hypotheses", 9));

        mvc.perform(get("/api/v1/incidents/{id}", incidentId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("INVESTIGATING"))
                .andExpect(jsonPath("$.stage.kind").value("RUNNING"))
                .andExpect(jsonPath("$.stage.waitingOn").value("WORKER"))
                .andExpect(jsonPath("$.stage.retryable").value(true));
    }
}
