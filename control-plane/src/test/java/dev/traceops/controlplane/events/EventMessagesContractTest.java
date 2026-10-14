package dev.traceops.controlplane.events;

import dev.traceops.controlplane.contracts.ContractValidator;
import dev.traceops.controlplane.contracts.ContractViolationException;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

import java.time.Instant;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * {@link EventMessages} 组装出来的东西必须过契约。
 *
 * <h2>这是那个真实事故的回归测试</h2>
 *
 * 人工创建的调查曾经传 {@code priority="manual"}，而 schema 的 enum 只有
 * {@code low/normal/high}。整整一个版本期内没有任何东西报错：Java 侧不校验、
 * Python 侧不校验、契约测试也没跑在那个路径上。最后是离线用
 * {@code contracts/tests} 的 Registry 手工校验才发现的。
 *
 * <p>现在这个测试就在**生产代码的调用点上**，所以同一类错误会在
 * 第一次跑测试时炸掉，而不是等到哪里去做一次离线体检。
 */
class EventMessagesContractTest {

    private static final Instant NOW = Instant.parse("2026-10-31T16:17:27Z");

    private static Map<String, Object> requested(String priority) {
        return EventMessages.investigationRequested(
                "inc-1", "checkout", "run-1", "demo", "cfg-1", priority, NOW);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> payloadOf(Map<String, Object> message) {
        return (Map<String, Object>) message.get("payload");
    }

    @ParameterizedTest(name = "priority={0}")
    @ValueSource(strings = {"low", "normal", "high"})
    @DisplayName("契约里允许的三档优先级都能通过")
    void acceptsContractPriorities(String priority) {
        assertThatCode(() -> requested(priority)).doesNotThrowAnyException();
    }

    @Test
    @DisplayName("回归：priority=manual 必须当场失败")
    void rejectsManualPriority() {
        assertThatThrownBy(() -> requested("manual"))
                .isInstanceOf(ContractViolationException.class)
                .hasMessageContaining("priority")
                .satisfies(exc -> org.assertj.core.api.Assertions
                        .assertThat(((ContractViolationException) exc).getViolations())
                        // 校验器的消息会指出字段路径（$.payload.priority）；
                        // 不假设它回显原值 —— 那是库的措辞，不是我们的契约。
                        .anyMatch(v -> v.contains("priority")));
    }

    @Test
    @DisplayName("告警原文必须出现在消息里 —— 否则模型永远读不到自然语言线索")
    void alertSummaryReachesThePayload() {
        Map<String, Object> message = EventMessages.investigationRequested(
                "inc-1", "checkout", "run-1", "demo", "cfg-1", "normal", NOW,
                NOW.minusSeconds(300), NOW, null,
                "checkout latency spike traced to cache tier: zulu-cache responses degraded");

        assertThat(payloadOf(message).get("alert_summary"))
                .isEqualTo("checkout latency spike traced to cache tier: "
                        + "zulu-cache responses degraded");
        assertThatCode(() -> ContractValidator.requireValid(
                EventMessages.INVESTIGATION_REQUESTED, message))
                .doesNotThrowAnyException();
    }

    @Test
    @DisplayName("没有告警原文时不写空串：\"有线索但为空\"和\"没有线索\"必须能区分")
    void blankAlertSummaryIsOmittedRatherThanSentAsEmptyString() {
        Map<String, Object> message = EventMessages.investigationRequested(
                "inc-1", "checkout", "run-1", "demo", "cfg-1", "normal", NOW,
                NOW.minusSeconds(300), NOW, null, "   ");

        assertThat(payloadOf(message)).doesNotContainKey("alert_summary");
    }

    @Test
    @DisplayName("组装结果与校验器看到的是同一份对象")
    void builtMessageIsWhatGetsValidated() {
        Map<String, Object> message = requested("normal");

        assertThatCode(() -> ContractValidator.requireValid(
                EventMessages.INVESTIGATION_REQUESTED, message))
                .doesNotThrowAnyException();
    }
}
