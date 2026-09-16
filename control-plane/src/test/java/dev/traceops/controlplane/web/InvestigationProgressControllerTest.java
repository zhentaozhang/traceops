package dev.traceops.controlplane.web;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import dev.traceops.controlplane.investigation.InvestigationProgress;
import dev.traceops.controlplane.investigation.InvestigationProgressStore;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

/**
 * 进度端点：快照 + SSE 流。
 *
 * <p>为什么要两个端点而不是只有 SSE：
 * SSE 只推**后续**变化。客户端在调查中途连上时，只订阅流会看到空白 ——
 * 而"等待中的空白"与"根本没有调查"在客户端看来是一样的，
 * 无法区分。快照端点解决这个歧义。
 */
@SpringBootTest
@AutoConfigureMockMvc
class InvestigationProgressControllerTest {

    @Autowired
    private MockMvc mvc;

    @Autowired
    private InvestigationProgressStore store;

    @Test
    void snapshotReturnsCurrentProgress() throws Exception {
        store.record("run-snap", new InvestigationProgress(
                "run-snap", "collect", 1, "collecting evidence", 3));

        mvc.perform(get("/api/v1/investigations/run-snap/progress"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.currentNode").value("collect"))
                .andExpect(jsonPath("$.stepIndex").value(1))
                .andExpect(jsonPath("$.evidenceCount").value(3));
    }

    @Test
    void unknownRunSnapshotIs404() throws Exception {
        // 404 表示"没有正在进行的进度"，调用方应先查事故再查进度
        mvc.perform(get("/api/v1/investigations/run-nope/progress"))
                .andExpect(status().isNotFound());
    }

    @Test
    void streamStartsAsAsyncAndEmitsSnapshotFirst() throws Exception {
        store.record("run-stream", new InvestigationProgress(
                "run-stream", "hypothesize", 2, "generating candidates", 5));

        MvcResult result = mvc.perform(get("/api/v1/investigations/run-stream/progress/stream"))
                .andExpect(request().asyncStarted())
                .andReturn();

        // 中途接入的客户端必须先拿到当前快照，否则要干等到下一个节点
        String body = result.getResponse().getContentAsString();
        assertThat(body).contains("hypothesize");
        assertThat(body).contains("event:progress").as("SSE events must be named");
        // 不调用 getAsyncResult：SSE 流是长连接，不会自行结束；
        // 等它结束等于等超时。断言"已进入 async 且首帧已发出"就够了。
    }

    @Test
    void streamWorksEvenWithoutExistingProgress() throws Exception {
        // 订阅早于任何进度是正常场景：连接应建立并等待，而不是报错
        mvc.perform(get("/api/v1/investigations/run-early/progress/stream"))
                .andExpect(request().asyncStarted());
    }
}
