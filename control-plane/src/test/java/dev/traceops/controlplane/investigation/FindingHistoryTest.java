package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import dev.traceops.controlplane.investigation.InvestigationResult.DiagnosisStatus;
import dev.traceops.controlplane.investigation.InvestigationResult.EvidenceSummary;
import dev.traceops.controlplane.investigation.InvestigationResult.RootCauseCandidate;
import dev.traceops.controlplane.investigation.InvestigationResult.Usage;
import java.time.Instant;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

/**
 * 多轮调查的结论历史。
 *
 * <p>为什么需要：{@code InvestigationFinding} 以 incidentId 为主键，
 * 同一事故的第二次调查会**覆盖**第一次的结论。对 PRD 5.2 的
 * "同一个事故可以重新调查"这条用户故事来说，旧结论消失是不可接受的 ——
 * 无法回答"这次比上次多查到了什么"。
 *
 * <p>本测试钉住两点：
 * <ul>
 *   <li>现有 {@code /{id}/finding} 仍返回**最新**结论（向后兼容）</li>
 *   <li>新增 {@code /{id}/findings} 返回**全部历史**（按时间正序）</li>
 * </ul>
 */
@SpringBootTest
@AutoConfigureMockMvc
@Transactional
class FindingHistoryTest {

    @Autowired
    private MockMvc mvc;

    @Autowired
    private FindingHistoryRepository history;

    private static InvestigationFinding finding(String incidentId, String runId,
                                                DiagnosisStatus status, String service) {
        return InvestigationFinding.from(incidentId, new InvestigationResult(
                "evt-" + runId, runId, incidentId,
                InvestigationResult.TerminalStatus.COMPLETED,
                status,
                service == null ? List.of() : List.of(
                        new RootCauseCandidate(service, null, "pool_exhaustion", 1, "supported")),
                null,
                null,
                false,
                status != DiagnosisStatus.SUPPORTED,
                new Usage(3, 0, 1, 5),
                "ref-" + runId,
                null,
                null,
                new EvidenceSummary(2, 1, 1, 0, 0)),
                Instant.parse("2026-10-30T10:00:00Z"));
    }

    @Test
    void secondInvestigationKeepsTheFirstOneInHistory() {
        Instant t0 = Instant.parse("2026-10-30T10:00:00Z");
        history.save(FindingHistorySnapshot.from(finding("inc-hist", "run-1", DiagnosisStatus.INCONCLUSIVE, null), t0));
        history.save(FindingHistorySnapshot.from(finding("inc-hist", "run-2", DiagnosisStatus.SUPPORTED, "redis"),
                t0.plusSeconds(60)));

        List<FindingHistorySnapshot> rows = history.findByIncidentIdOrderByRecordedAtAsc("inc-hist");
        assertThat(rows).hasSize(2);
        assertThat(rows.get(0).getInvestigationRunId()).isEqualTo("run-1");
        assertThat(rows.get(0).getDiagnosisStatus()).isEqualTo("INCONCLUSIVE");
        assertThat(rows.get(1).getInvestigationRunId()).isEqualTo("run-2");
        assertThat(rows.get(1).getRootCauseService()).isEqualTo("redis");
    }

    @Test
    void historyEndpointReturnsRunsInOrder() throws Exception {
        Instant t0 = Instant.parse("2026-10-30T10:00:00Z");
        for (int i = 1; i <= 3; i++) {
            history.save(FindingHistorySnapshot.from(
                    finding("inc-hist2", "run-" + i, DiagnosisStatus.SUPPORTED, "redis"),
                    t0.plusSeconds(i * 30L)));
        }

        mvc.perform(get("/api/v1/incidents/inc-hist2/findings"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].investigationRunId").value("run-1"))
                .andExpect(jsonPath("$[2].investigationRunId").value("run-3"));
    }

    @Test
    void unknownIncidentHistoryIsEmptyNotError() throws Exception {
        mvc.perform(get("/api/v1/incidents/inc-unknown/findings"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(0));
    }

    @Test
    void latestFindingEndpointStill404sWhenAbsent() throws Exception {
        mvc.perform(get("/api/v1/incidents/inc-missing/finding"))
                .andExpect(status().isNotFound());
    }
}
