package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import org.junit.jupiter.api.Test;

/**
 * 调查进度的存储与查询。
 *
 * <p>背景：poller 收到 {@code investigation.updated} 后只计了一个数就 ACK 掉了，
 * **进度内容被丢弃**。于是"实时进度"这条 P2 能力上一步只补齐了数据产生侧，
 * 消费侧仍然无处可读 —— SSE 端点没有数据源。
 *
 * <p>本测试钉住存储语义：
 * <ul>
 *   <li>每次进度**覆盖**同一 run 的当前状态（进度是状态，不是流水）</li>
 *   <li>按 run_id 查询最新进度</li>
 *   <li>能判断一个 run 是否"正在调查"</li>
 * </ul>
 *
 * <p>为什么用"覆盖"而不是"追加"：SSE 关心的是"现在到哪一步了"。
 * 追加会让一个跑了 7 个节点的调查积累 7 条记录，而客户端每次只需要最后一条；
 * 历史进度属于审计中心（`evidence/audit.py`）的职责，不该混在这里。
 */
class InvestigationProgressTest {

    @Test
    void storesLatestProgressPerRun() {
        InvestigationProgressStore store = new InvestigationProgressStore();

        store.record("run-1", new InvestigationProgress(
                "run-1", "collect", 1, "collecting evidence", 3));

        assertThat(store.latest("run-1")).isPresent();
        assertThat(store.latest("run-1").get().currentNode()).isEqualTo("collect");
        assertThat(store.latest("run-1").get().evidenceCount()).isEqualTo(3);
    }

    @Test
    void laterProgressOverwritesEarlier() {
        InvestigationProgressStore store = new InvestigationProgressStore();
        store.record("run-1", new InvestigationProgress("run-1", "plan", 0, "planning", 0));
        store.record("run-1", new InvestigationProgress("run-1", "report", 6, "assembling", 9));

        InvestigationProgress latest = store.latest("run-1").orElseThrow();
        assertThat(latest.currentNode()).isEqualTo("report");
        assertThat(latest.stepIndex()).isEqualTo(6);
        assertThat(store.count()).as("progress is state, not a log").isEqualTo(1);
    }

    @Test
    void outOfOrderProgressDoesNotGoBackwards() {
        // SSE 消息可能乱序到达；旧进度不能覆盖新进度，否则前端会看到"倒退"
        InvestigationProgressStore store = new InvestigationProgressStore();
        store.record("run-1", new InvestigationProgress("run-1", "rank", 5, "ranking", 9));
        store.record("run-1", new InvestigationProgress("run-1", "plan", 0, "planning", 0));

        assertThat(store.latest("run-1").orElseThrow().stepIndex())
                .as("a stale progress update must not rewind the view").isEqualTo(5);
    }

    @Test
    void unknownRunHasNoProgress() {
        assertThat(new InvestigationProgressStore().latest("run-missing")).isEmpty();
    }

    @Test
    void progressCanBeClearedWhenRunFinishes() {
        // 终态到了就该清掉进度，否则"正在调查"的判断会永远为真
        InvestigationProgressStore store = new InvestigationProgressStore();
        store.record("run-1", new InvestigationProgress("run-1", "report", 6, "assembling", 9));
        store.clear("run-1");
        assertThat(store.latest("run-1")).isEmpty();
    }

    @Test
    void subscribersAreNotifiedOfProgress() {
        InvestigationProgressStore store = new InvestigationProgressStore();
        List<InvestigationProgress> seen = new java.util.ArrayList<>();
        store.subscribe("run-1", seen::add);

        store.record("run-1", new InvestigationProgress("run-1", "collect", 1, "collecting", 2));

        assertThat(seen).hasSize(1);
        assertThat(seen.get(0).currentNode()).isEqualTo("collect");
    }

    @Test
    void subscriberOnlySeesItsOwnRun() {
        InvestigationProgressStore store = new InvestigationProgressStore();
        List<InvestigationProgress> seen = new java.util.ArrayList<>();
        store.subscribe("run-1", seen::add);
        store.record("run-2", new InvestigationProgress("run-2", "collect", 1, "collecting", 2));

        assertThat(seen).isEmpty();
    }
}
