package dev.traceops.controlplane.web;

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
 * 一次 webhook 里的**每一条**告警都必须被处理。
 *
 * <p>缺陷：{@code AlertController.java:25} 取的是 {@code request.alerts().getFirst()}，
 * 于是同一批里的其余告警被**静默丢弃** —— 不报错、不计数、不入库。
 *
 * <p>这不是"少处理一条"，而是直接切断了多故障场景的数据来源：
 * EVALUATION 里 F-007（两条独立链同时故障）之所以只能看到一条链，
 * 根因写法是"要真正覆盖多故障需要多告警接入"，
 * 而实际上**多告警一直送到了，只是被丢掉**。
 *
 * <p>Alertmanager 的 webhook 按设计就是**批量**的（一个 group 多条 alert），
 * 所以这不是边界情况，是主路径。
 */
@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class AlertBatchIngestTest {

    @Autowired
    private MockMvc mvc;

    @Test
    void batchWithTwoDistinctAlertsCreatesTwoIncidents() throws Exception {
        String body = """
                {
                  "alerts": [
                    {"fingerprint":"fp-batch-a","status":"firing","service":"order-service",
                     "severity":"critical","summary":"redis pool exhausted"},
                    {"fingerprint":"fp-batch-b","status":"firing","service":"billing-api",
                     "severity":"warning","summary":"dependency timeout"}
                  ]
                }
                """;

        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                // 响应必须报告"处理了几条"，否则丢弃是不可见的
                .andExpect(jsonPath("$.processed").value(2));
    }

    @Test
    void batchWithDuplicateFingerprintsStillProcessesAll() throws Exception {
        // 同批内重复指纹：两条都要计数（一条建、一条去重），不能只处理第一条
        String body = """
                {
                  "alerts": [
                    {"fingerprint":"fp-batch-dup","status":"firing","service":"order-service",
                     "severity":"critical","summary":"pool exhausted"},
                    {"fingerprint":"fp-batch-dup","status":"firing","service":"order-service",
                     "severity":"critical","summary":"pool exhausted"}
                  ]
                }
                """;

        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.processed").value(2));
    }

    @Test
    void singleAlertBatchStillWorks() throws Exception {
        String body = """
                {"alerts":[{"fingerprint":"fp-single","status":"firing","service":"order-service",
                            "severity":"critical","summary":"pool exhausted"}]}
                """;
        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.processed").value(1));
    }
}
