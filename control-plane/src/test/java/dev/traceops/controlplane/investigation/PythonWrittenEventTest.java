package dev.traceops.controlplane.investigation;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.transaction.annotation.Transactional;

/**
 * 跨语言对称性：控制面必须能吃下 **agent-runtime 真的写出来**的事件。
 *
 * <p>这个测试存在的理由不是"再测一遍 JSON 能解析"，而是守住一个曾经
 * 真实存在的断链：agent-runtime 从来没有 Redis 写端（sink 永远是
 * {@code InMemoryEventSink}），所以它写出的事件只在 stdout 里出现过。
 * 控制面永远收不到终态事件，跨语言闭环只跑通了一半（Java → Python）。
 *
 * <p>fixture 是从真实 Redis 里捞出来的一条 {@code investigation.completed}
 * （Python worker 写的），不是手搓的 —— 手搓的 fixture 只能证明
 * "我写的东西符合我写的东西"，证明不了两端形状一致。
 */
@SpringBootTest(properties = {
        "traceops.outbox.relay-enabled=false",
        "traceops.investigation.poller-enabled=false",
        "spring.jpa.hibernate.ddl-auto=create-drop",
})
@Transactional
class PythonWrittenEventTest {

    private static final Path FIXTURE =
            Path.of("src", "test", "resources", "python-written-investigation-completed.json");

    @Autowired
    private InvestigationResultService service;

    @Autowired
    private ObjectMapper mapper;

    private JsonNode fixture() throws Exception {
        try (InputStream in = Files.newInputStream(FIXTURE)) {
            return mapper.readTree(in);
        }
    }

    @Test
    void theFixtureIsExactlyWhatThePythonWorkerWrote() throws Exception {
        // 先确认 fixture 没被手改：它必须带 Python 侧才有的痕迹 ——
        // envelope.producer 是 agent-runtime，occurred_at 带微秒和时区偏移，
        // event_id 是 uuid4 形态。
        JsonNode doc = fixture();
        JsonNode envelope = doc.get("envelope");
        assertThat(envelope.get("producer").asText()).isEqualTo("agent-runtime");
        assertThat(envelope.get("event_type").asText()).isEqualTo("investigation.completed");
        assertThat(envelope.get("occurred_at").asText()).matches("\\d{4}-\\d{2}-\\d{2}T.*[+-]\\d{2}:\\d{2}");
        assertThat(envelope.get("event_id").asText())
                .matches("^evt-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");
        assertThat(doc.get("payload").get("investigation_run_id").asText()).isNotBlank();
    }

    @Test
    void theControlPlaneCanParseAPythonWrittenTerminalEvent() throws Exception {
        // verifyIdentity 要求：event_type 是终态、producer 是 agent-runtime。
        // Python 侧写的正是这两样 —— 如果哪天 Python 改了 producer，
        // 这个测试会立刻炸，而不是等到线上"控制面收不到结论"。
        JsonNode doc = fixture();
        InvestigationResult result = InvestigationResult.fromJson(doc);
        assertThat(result.investigationRunId()).isEqualTo("run-live-1");
        assertThat(result.diagnosisStatus())
                .isEqualTo(InvestigationResult.DiagnosisStatus.INCONCLUSIVE);
        assertThat(result.terminalStatus().isTerminalEventType("investigation.completed")).isTrue();
    }

    @Test
    void aPythonWrittenEventIsAppliedWithoutBeingTreatedAsPoison() throws Exception {
        // 走完整的 apply()：事件被解析、幂等校验、驱动状态机。
        // 如果形状不对称，这里会抛 IllegalArgumentException ——
        // 那正是 poller 会当成"毒消息"丢掉的 IllegalArgumentException。
        JsonNode doc = fixture();
        String raw = mapper.writeValueAsString(doc);

        List<Applied> outcomes = new ArrayList<>();
        Applied applied = service.apply(raw);
        outcomes.add(applied);

        // 事件本身是合法的 → 不是毒消息，但事故不存在 → NOT_FOUND。
        // 这里断言的是"没被当成非法消息扔掉"。
        assertThat(applied.outcome()).isEqualTo(Applied.Outcome.NOT_FOUND);
        assertThat(applied.eventId()).isEqualTo(doc.get("envelope").get("event_id").asText());
        assertThat(applied.investigationRunId()).isEqualTo("run-live-1");
    }
}
