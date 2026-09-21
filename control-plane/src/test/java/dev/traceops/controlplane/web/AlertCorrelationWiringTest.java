package dev.traceops.controlplane.web;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

/**
 * 告警关联必须真的**接进生产路径**。
 *
 * <p>缺陷：{@code AlertCorrelation} 有 12 个测试、API 完整
 * （{@code shouldCorrelate} / {@code group}，含传递关联的 union-find 分组），
 * 但**生产 main 代码零引用** —— 只有测试引用它。
 * 也就是说：关联能力被完整实现、完整测试、然后**从未被调用**。
 *
 * <p>这与前面三个"声称完成但不成立"是同一种病：测试通过 ≠ 能力存在。
 *
 * <p>正确行为：批量接入时按关联关系分组 ——
 * 同指纹/同服务时间窗/依赖服务时间窗内的告警归入同一事故，
 * 互不相关的各自成事故。
 */
@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class AlertCorrelationWiringTest {

    @Autowired
    private MockMvc mvc;

    @Test
    void relatedAlertsInOneBatchShareOneIncident() throws Exception {
        // 同一服务的两条告警（指纹不同、时间相近）→ 关联到同一事故
        String body = """
                {"alerts":[
                  {"fingerprint":"fp-rel-1","status":"firing","service":"order-service",
                   "severity":"critical","summary":"pool exhausted","startsAt":"2026-10-30T10:02:00Z"},
                  {"fingerprint":"fp-rel-2","status":"firing","service":"order-service",
                   "severity":"warning","summary":"latency high","startsAt":"2026-10-30T10:03:00Z"}
                ]}
                """;
        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.processed").value(2))
                // 关联成功 → 只建 1 个事故
                .andExpect(jsonPath("$.created").value(1))
                .andExpect(jsonPath("$.incidentIds.length()").value(2));
    }

    @Test
    void unrelatedAlertsInOneBatchCreateSeparateIncidents() throws Exception {
        // 不同服务、无依赖关系、时间相近 → 不该被合并
        String body = """
                {"alerts":[
                  {"fingerprint":"fp-unrel-1","status":"firing","service":"order-service",
                   "severity":"critical","summary":"pool exhausted","startsAt":"2026-10-30T10:02:00Z"},
                  {"fingerprint":"fp-unrel-2","status":"firing","service":"billing-api",
                   "severity":"critical","summary":"dependency timeout","startsAt":"2026-10-30T10:02:00Z"}
                ]}
                """;
        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.processed").value(2))
                .andExpect(jsonPath("$.created").value(2));
    }

    @Test
    void dependentServiceAlertsAreCorrelated() throws Exception {
        // order-service 依赖 redis：两边的告警时间窗内应当关联
        String body = """
                {"alerts":[
                  {"fingerprint":"fp-dep-1","status":"firing","service":"redis",
                   "severity":"critical","summary":"pool exhausted","startsAt":"2026-10-30T10:02:00Z"},
                  {"fingerprint":"fp-dep-2","status":"firing","service":"order-service",
                   "severity":"warning","summary":"redis timeout","startsAt":"2026-10-30T10:03:00Z"}
                ],"dependencies":[["order-service","redis"]]}
                """;
        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.created").value(1));
    }

    @Test
    void singleAlertIsUnchanged() throws Exception {
        String body = """
                {"alerts":[{"fingerprint":"fp-one","status":"firing","service":"order-service",
                            "severity":"critical","summary":"pool exhausted"}]}
                """;
        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.created").value(1));
    }
}
