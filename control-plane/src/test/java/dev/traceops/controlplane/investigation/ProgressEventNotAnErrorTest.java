package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

/**
 * 进度事件不是错误。
 *
 * <p>背景：Python 侧现在每个调查节点发一条 {@code investigation.updated}
 * （带 {@code progress.step_index}）。而 {@code InvestigationResult.from}
 * 只接受 completed/failed 两种 event_type，于是：
 *
 * <pre>
 *   else -> throw IllegalArgumentException("unexpected event_type")
 * </pre>
 *
 * <p>poll 的 catch 把这个 IllegalArgumentException 记成
 * "discarding unusable investigation message" + errors++ + ACK。
 * 也就是说，**每次调查会往日志里刷 N 条"坏消息"告警**，
 * 而那只是正常的进度事件。运维会去排查一堆不存在的契约违约。
 *
 * <p>正确行为：进度事件应当被**显式识别为进度**（不是终态、不是错误），
 * 计数为"进度事件"而不是 errors。
 */
class ProgressEventNotAnErrorTest {

    private static JsonNode node(String json) {
        try {
            return new ObjectMapper().readTree(json);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    @Test
    void updatedEventIsRejectedByTheTerminalParser() {
        // 这不矛盾：InvestigationResult 是**终态**专用模型，
        // 它拒绝进度事件是对的（不能让状态机被进度驱动）。
        // 关键是 poller 必须先识别出"这是进度"再跳过，
        // 而不是让异常落到 catch 里被记成"坏消息"。
        String json = """
                {
                  "envelope": {
                    "event_id": "evt-p1",
                    "event_type": "investigation.updated",
                    "schema_version": "1.0",
                    "occurred_at": "2026-10-30T10:00:00Z",
                    "producer": "agent-runtime",
                    "correlation_id": "inc-1",
                    "aggregate_id": "run-1",
                    "aggregate_version": 2,
                    "causation_id": null
                  },
                  "payload": {
                    "investigation_run_id": "run-1",
                    "status": "RUNNING",
                    "aggregate_version": 2,
                    "current_node": "collect",
                    "progress": {"step_index": 1, "step_description": "collecting",
                                 "evidence_count": 2}
                  }
                }
                """;

        assertThatThrownBy(() -> InvestigationResult.fromJson(node(json)))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("investigation.updated");
    }

    @Test
    void completedEventStillParsesAsTerminal() {
        String json = """
                {
                  "envelope": {
                    "event_id": "evt-c1", "event_type": "investigation.completed",
                    "schema_version": "1.0", "occurred_at": "2026-10-30T10:00:00Z",
                    "producer": "agent-runtime", "correlation_id": "inc-1",
                    "aggregate_id": "run-1", "aggregate_version": 5, "causation_id": null
                  },
                  "payload": {
                    "investigation_run_id": "run-1", "diagnosis_status": "SUPPORTED",
                    "rca_report_ref": "run-1/report", "abstained": false,
                    "root_cause_candidates": [], "evidence_summary": {"total": 2},
                    "usage": {"duration_seconds": 1, "rounds": 1, "tokens": 0, "tool_calls": 3}
                  }
                }
                """;
        InvestigationResult result = InvestigationResult.fromJson(node(json));
        assertThat(result.terminalStatus()).isEqualTo(InvestigationResult.TerminalStatus.COMPLETED);
    }
}
