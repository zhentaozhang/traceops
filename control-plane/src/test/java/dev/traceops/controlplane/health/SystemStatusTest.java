package dev.traceops.controlplane.health;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.service.IncidentService;
import dev.traceops.controlplane.service.IncidentWriter;
import dev.traceops.controlplane.web.ManualIncidentRequest;
import java.time.Instant;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

/**
 * 系统状态端点的真实链路测试。
 *
 * <p>这些断言都指向同一件事：**计数器必须真的被读到**。仓库里刚加过的
 * {@code evictedRuns}/{@code droppedSubscribers} 在加完之后没有任何读者，
 * 一个没人读的计数既不能报警也不能解释故障 —— 它只是让代码看起来更完备。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop"
})
@AutoConfigureMockMvc
@Transactional
class SystemStatusTest {

    @Autowired
    private SystemStatusService status;

    @Autowired
    private IncidentService incidentService;

    @Autowired
    private IncidentRepository incidents;

    @Autowired
    private IncidentWriter writer;

    @Autowired
    private MockMvc mvc;

    @SuppressWarnings("unchecked")
    private Map<String, Object> component(Map<String, Object> snapshot, String name) {
        return (Map<String, Object>) ((Map<String, Object>) snapshot.get("components")).get(name);
    }

    /** 三个条目都必须上报过：声明了却沉默的条目说明有人忘了接数据源。 */
    @Test
    void everyClaimedEntryReportsAndNoneAreSilent() {
        Map<String, Object> snapshot = status.snapshot(Instant.now());

        assertThat((java.util.List<?>) snapshot.get("claimedButSilent")).isEmpty();
        assertThat(component(snapshot, SystemStatusService.OUTBOX)).isNotNull();
        assertThat(component(snapshot, SystemStatusService.RETRIES)).isNotNull();
        assertThat(component(snapshot, SystemStatusService.PROGRESS_STORE)).isNotNull();
    }

    /** 刚写入的 outbox 事件不该被判降级 —— 判据是"等了多久"，不是"有几条"。 */
    @Test
    void aFreshOutboxBacklogIsHealthyButVisible() {
        incidentService.createManual(new ManualIncidentRequest(
                "order-service", "checkout errors", "warning", "synthetic status test"));

        Map<String, Object> outbox = component(status.snapshot(Instant.now()), SystemStatusService.OUTBOX);

        assertThat(outbox.get("severity")).isEqualTo("OK");
        assertThat((String) outbox.get("detail")).contains("pending");
    }

    /**
     * 一条排期已过、却仍挂在事故上的重试，是"排了期但没人发出去"的唯一外部证据。
     * 它必须让整体降级，而不是被当成"正在重试中"的常态。
     */
    @Test
    void anOverdueRetryDegradesTheRetryComponent() {
        var created = incidentService.createManual(new ManualIncidentRequest(
                "order-service", "checkout errors", "warning", "synthetic retry test"));
        Incident incident = incidents.findById(created.incidentId()).orElseThrow();
        incident.scheduleRetryAt(Instant.now().minusSeconds(30));
        writer.save(incident);

        Map<String, Object> snapshot = status.snapshot(Instant.now());
        Map<String, Object> retries = component(snapshot, SystemStatusService.RETRIES);

        assertThat(retries.get("severity")).isEqualTo("DEGRADED");
        assertThat((String) retries.get("detail")).contains("never dispatched");
        assertThat(snapshot.get("worst")).isEqualTo("DEGRADED");
    }

    /** 排期在未来的重试是正常状态，不能报警 —— 否则真正的故障会被常态噪音淹没。 */
    @Test
    void aFutureRetryIsHealthy() {
        var created = incidentService.createManual(new ManualIncidentRequest(
                "order-service", "checkout errors", "warning", "synthetic retry test"));
        Incident incident = incidents.findById(created.incidentId()).orElseThrow();
        incident.scheduleRetryAt(Instant.now().plusSeconds(30));
        writer.save(incident);

        Map<String, Object> retries = component(
                status.snapshot(Instant.now()), SystemStatusService.RETRIES);

        assertThat(retries.get("severity")).isEqualTo("OK");
        assertThat((String) retries.get("detail")).contains("waiting for backoff");
    }

    /** 进度存储的计数器必须出现在读数里，否则它们仍然是装饰。 */
    @Test
    void theProgressStoreCountersAreActuallyRead() {
        Map<String, Object> store = component(
                status.snapshot(Instant.now()), SystemStatusService.PROGRESS_STORE);

        String detail = (String) store.get("detail");
        assertThat(detail).contains("runs evicted").contains("subscribers dropped")
                .contains("live subscribers");
    }

    @Test
    void theEndpointServesTheSnapshot() throws Exception {
        mvc.perform(get("/api/v1/status"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.worst").exists())
                .andExpect(jsonPath("$.checkedAt").exists())
                .andExpect(jsonPath("$.components['outbox.delivery'].owner").value("control-plane"))
                .andExpect(jsonPath("$.components['outbox.delivery'].severity").exists());
    }
}
