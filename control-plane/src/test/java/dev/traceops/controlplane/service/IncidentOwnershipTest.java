package dev.traceops.controlplane.service;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.domain.IncidentStatus;
import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

/**
 * "事故没有负责人"曾经的后果不是缺一个字段，而是三条读数一起失真：
 * {@code stage} 说在等人却指不出人、结论没人确认过、记忆里全是未验证的猜测。
 */
@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class IncidentOwnershipTest {

    @Autowired
    private IncidentRepository incidents;
    @Autowired
    private IncidentOwnershipService ownership;
    @Autowired
    private MockMvc mvc;

    private Incident diagnosed() {
        Incident incident = Incident.openManual("inc-own-1", "checkout-service",
                "latency spike", "high", Instant.now());
        incident.transitionTo(IncidentStatus.INVESTIGATING, Instant.now());
        incident.transitionTo(IncidentStatus.DIAGNOSED, Instant.now());
        return incidents.save(incident);
    }

    private Incident fresh() {
        return incidents.save(Incident.openManual("inc-own-2", "payment-service",
                "errors", "high", Instant.now()));
    }

    @Test
    void assigningANameIsRecordedAndShown() throws Exception {
        Incident incident = diagnosed();

        mvc.perform(post("/api/v1/incidents/" + incident.getId() + "/assign")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"assignee\":\"chen\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.assignee").value("chen"));

        mvc.perform(get("/api/v1/incidents/" + incident.getId()))
                .andExpect(jsonPath("$.assignee").value("chen"));
    }

    @Test
    void aBlankAssigneeIsRefusedBecauseItLooksLikeSomeoneIsThere() {
        Incident incident = diagnosed();
        assertThatThrownBy(() -> ownership.assign(incident.getId(), "   "))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("must not be blank");
    }

    @Test
    void acknowledgingIsIdempotentSoHowLongSomeoneHasOwnedItStaysTrue() {
        Incident incident = diagnosed();
        ownership.assign(incident.getId(), "chen");

        Incident first = ownership.acknowledge(incident.getId());
        Instant firstAck = first.getAcknowledgedAt();
        assertThat(firstAck).isNotNull();

        Incident second = ownership.acknowledge(incident.getId());
        assertThat(second.getAcknowledgedAt()).isEqualTo(firstAck);
    }

    @Test
    void theStageSaysWhoIsBeingWaitedFor() throws Exception {
        Incident incident = diagnosed();
        ownership.assign(incident.getId(), "chen");

        mvc.perform(get("/api/v1/incidents/" + incident.getId()))
                .andExpect(jsonPath("$.stage.waitingOn").value("PERSON"))
                .andExpect(jsonPath("$.stage.detail").value(
                        org.hamcrest.Matchers.containsString("assigned to chen")));
    }

    @Test
    void anUnassignedIncidentSaysSoInsteadOfPretending() throws Exception {
        Incident incident = diagnosed();
        mvc.perform(get("/api/v1/incidents/" + incident.getId()))
                .andExpect(jsonPath("$.stage.detail").value(
                        org.hamcrest.Matchers.containsString("nobody is assigned")));
    }

    @Test
    void confirmingADiagnosisRecordsWhoAndWhat() throws Exception {
        Incident incident = diagnosed();

        mvc.perform(post("/api/v1/incidents/" + incident.getId() + "/confirm")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"accepted\":true,\"by\":\"chen\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.confirmation").value("ACCEPTED"))
                .andExpect(jsonPath("$.confirmedBy").value("chen"));

        Incident stored = incidents.findById(incident.getId()).orElseThrow();
        assertThat(stored.getConfirmedAt()).isNotNull();
        // 确认不推进状态：接受一个诊断不等于事故发生已经过去。
        assertThat(stored.getStatus()).isEqualTo(IncidentStatus.DIAGNOSED);
    }

    @Test
    void rejectingIsRecordedAsRejectedNotAsMissing() {
        Incident incident = diagnosed();
        ownership.confirm(incident.getId(), "chen", false);
        assertThat(incidents.findById(incident.getId()).orElseThrow().getConfirmation())
                .isEqualTo("REJECTED");
    }

    @Test
    void anIncidentWithoutAConclusionCannotBeConfirmed() throws Exception {
        Incident incident = fresh();
        mvc.perform(post("/api/v1/incidents/" + incident.getId() + "/confirm")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"accepted\":true,\"by\":\"chen\"}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.detail").value(
                        org.hamcrest.Matchers.containsString("only a DIAGNOSED incident")));
    }

    @Test
    void aConfirmationCannotBeRewritten() throws Exception {
        Incident incident = diagnosed();
        ownership.confirm(incident.getId(), "chen", true);

        mvc.perform(post("/api/v1/incidents/" + incident.getId() + "/confirm")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"accepted\":false,\"by\":\"dana\"}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.detail").value(
                        org.hamcrest.Matchers.containsString("cannot be rewritten")));
    }

    @Test
    void confirmingRequiresAName() throws Exception {
        Incident incident = diagnosed();
        mvc.perform(post("/api/v1/incidents/" + incident.getId() + "/confirm")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"accepted\":true,\"by\":\"\"}"))
                .andExpect(status().isBadRequest());
    }

    @Test
    void theStageOfAResolvedIncidentSaysWhetherTheConclusionWasConfirmed() throws Exception {
        Incident incident = diagnosed();
        ownership.confirm(incident.getId(), "chen", true);
        incident.transitionTo(IncidentStatus.RESOLVED, Instant.now());
        incidents.save(incident);

        mvc.perform(get("/api/v1/incidents/" + incident.getId()))
                .andExpect(jsonPath("$.stage.detail").value(
                        org.hamcrest.Matchers.containsString("diagnosis accepted")));
    }
}
