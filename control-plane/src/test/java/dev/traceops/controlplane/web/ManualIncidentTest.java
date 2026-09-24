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
 * 手动创建事故（PRD 5.1，此前 ⬜）。
 *
 * <p>为什么需要：告警不是事故的唯一来源。SRE 观察到用户投诉、或复盘时发现
 * "这次故障没触发告警"，都需要能手工立一个事故并启动调查。
 * 没有这个入口时，唯一的创建路径是等告警 —— 而"没有告警的故障"恰恰是最难查的。
 *
 * <p>关键设计点：**人工事故没有 fingerprint**。
 *
 * <p>告警靠 fingerprint 去重（同一告警重复推送只建一个事故）。
 * 人工创建没有告警，因此：
 * <ul>
 *   <li>{@code fingerprint} 必须可为空 —— 硬塞一个假指纹会和告警去重逻辑混淆</li>
 *   <li>去重查询必须排除人工事故 —— 否则两条没有指纹的人工事故会互相"去重"
 *       而只留一条，用户会以为创建失败了</li>
 *   <li>必须记录 {@code origin}，否则事后无法区分"告警来的"和"人来的"</li>
 * </ul>
 */
@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class ManualIncidentTest {

    @Autowired
    private MockMvc mvc;

    @Test
    void manualIncidentIsCreatedAndInvestigated() throws Exception {
        String body = """
                {"service":"order-service","title":"user reports checkout hanging",
                 "severity":"warning","reason":"observed by SRE, no alert fired"}
                """;
        mvc.perform(post("/api/v1/incidents").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.incidentId").isNotEmpty())
                // 创建即进入调查（不是停在 OPEN）：人工事故的整个价值是触发一次调查，
                // 停在 OPEN 等于让人建完还要再点一次
                .andExpect(jsonPath("$.status").value("INVESTIGATING"))
                // 人工事故必须标明来源，否则事后分不清是人建的还是告警来的
                .andExpect(jsonPath("$.origin").value("MANUAL"));
    }

    @Test
    void manualIncidentsAreNotDeduplicated() throws Exception {
        // 两次手工创建同一服务同一标题，都是用户主动行为 —— 不能被指纹去重吞掉
        String body = """
                {"service":"order-service","title":"checkout hanging",
                 "severity":"warning","reason":"two separate observations"}
                """;
        String first = mvc.perform(post("/api/v1/incidents")
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
        String second = mvc.perform(post("/api/v1/incidents")
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();

        String firstId = first.replaceAll(".*\"incidentId\":\"(.*?)\".*", "$1");
        String secondId = second.replaceAll(".*\"incidentId\":\"(.*?)\".*", "$1");
        org.assertj.core.api.Assertions.assertThat(firstId)
                .as("manual creations must not swallow each other")
                .isNotEqualTo(secondId);
    }

    @Test
    void manualIncidentStartsAnInvestigation() throws Exception {
        // 只建事故不调查等于没用 —— 人工事故的整个价值就是"让人能触发一次调查"
        String body = """
                {"service":"order-service","title":"manual probe","severity":"warning",
                 "reason":"needs investigation"}
                """;
        mvc.perform(post("/api/v1/incidents").contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.investigationRunId").isNotEmpty());
    }

    @Test
    void manualIncidentRequiresServiceAndTitle() throws Exception {
        String badService = """
                {"title":"no service","severity":"warning","reason":"x"}
                """;
        mvc.perform(post("/api/v1/incidents").contentType(MediaType.APPLICATION_JSON).content(badService))
                .andExpect(status().isBadRequest());

        String badTitle = """
                {"service":"order-service","severity":"warning","reason":"x"}
                """;
        mvc.perform(post("/api/v1/incidents").contentType(MediaType.APPLICATION_JSON).content(badTitle))
                .andExpect(status().isBadRequest());
    }

    @Test
    void alertDrivenIncidentStillRecordsItsOwnOrigin() throws Exception {
        // 告警创建的事故 origin 必须是 ALERT，两种来源可区分
        String alert = """
                {"alerts":[{"fingerprint":"fp-origin-1","status":"firing",
                 "service":"order-service","severity":"critical","summary":"pool exhausted"}]}
                """;
        mvc.perform(post("/api/v1/alerts").contentType(MediaType.APPLICATION_JSON).content(alert))
                .andExpect(status().isAccepted());

        mvc.perform(get("/api/v1/incidents").param("service", "order-service"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].origin").value("ALERT"));
    }
}
