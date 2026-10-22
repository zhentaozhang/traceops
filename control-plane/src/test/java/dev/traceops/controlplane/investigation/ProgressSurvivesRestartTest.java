package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.transaction.annotation.Transactional;

/**
 * 控制面重启时 worker 往往还在跑 —— 进度不能跟着内存一起消失。
 *
 * <p>这个测试模拟"重启"的方式是丢掉旧的 store 实例、用同一个 repository 新建一个，
 * 而不是真的重启进程：要验的正是"内存里没有时能不能从库里读回来"。
 */
@SpringBootTest(properties = {"traceops.outbox.relay-enabled=false"})
@Transactional
class ProgressSurvivesRestartTest {

    @Autowired
    private InvestigationProgressStore store;

    @Autowired
    private InvestigationProgressRepository rows;

    @Test
    void progressRecordedBeforeARestartIsVisibleAfterIt() {
        String runId = "run-restart-1";
        store.record(runId, new InvestigationProgress(runId, "correlate", 4, "correlating signals", 7));

        InvestigationProgressStore afterRestart = new InvestigationProgressStore(rows);

        Optional<InvestigationProgress> restored = afterRestart.latest(runId);
        assertThat(restored).isPresent();
        assertThat(restored.get().currentNode()).isEqualTo("correlate");
        assertThat(restored.get().stepIndex()).isEqualTo(4);
        assertThat(restored.get().evidenceCount()).isEqualTo(7);
    }

    @Test
    void clearingAFinishedRunRemovesTheRowAsWellAsTheCache() {
        String runId = "run-restart-2";
        store.record(runId, new InvestigationProgress(runId, "report", 7, "writing the report", 9));
        assertThat(rows.findById(runId)).isPresent();

        store.clear(runId);

        assertThat(rows.findById(runId)).isEmpty();
        assertThat(new InvestigationProgressStore(rows).latest(runId)).isEmpty();
    }

    @Test
    void aRepeatedStepDoesNotMoveTheStoredStepBackwards() {
        String runId = "run-restart-3";
        store.record(runId, new InvestigationProgress(runId, "rank", 6, "ranking hypotheses", 5));
        store.record(runId, new InvestigationProgress(runId, "collect", 2, "stale duplicate", 1));

        InvestigationProgress restored = new InvestigationProgressStore(rows).latest(runId).orElseThrow();
        assertThat(restored.stepIndex()).isEqualTo(6);
        assertThat(restored.currentNode()).isEqualTo("rank");
    }
}
