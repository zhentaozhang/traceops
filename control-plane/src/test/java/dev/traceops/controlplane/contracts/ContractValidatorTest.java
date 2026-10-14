package dev.traceops.controlplane.contracts;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 契约校验器自身的行为。
 *
 * <p>这些测试不是"再测一遍 schema"——schema 的正确性由
 * {@code contracts/tests/test_contracts.py} 负责。这里要证明的是：
 * <b>Java 侧真的把 schema 加载起来了，并且真的会拒绝坏消息</b>。
 * 一个悄悄放行一切的校验器比没有校验器更糟：它会让人以为契约是有效的。
 */
class ContractValidatorTest {

    private static Map<String, Object> envelope(String eventType) {
        Map<String, Object> envelope = new LinkedHashMap<>();
        envelope.put("event_id", "evt-ba914660-f4a3-4019-97b1-1b990e66ea0f");
        envelope.put("event_type", eventType);
        envelope.put("schema_version", "1.0");
        envelope.put("occurred_at", "2026-10-31T16:17:27.774940+00:00");
        envelope.put("correlation_id", "inc-1");
        envelope.put("causation_id", null);
        envelope.put("aggregate_id", "run-1");
        envelope.put("aggregate_version", 3);
        envelope.put("producer", "agent-runtime");
        return envelope;
    }

    private static Map<String, Object> completedMessage() {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("investigation_run_id", "run-1");
        payload.put("diagnosis_status", "INCONCLUSIVE");
        payload.put("abstained", true);
        payload.put("root_cause_candidates", List.of());
        payload.put("evidence_summary", Map.of("total", 0));
        payload.put("rca_report_ref", "run-1/report");
        payload.put("usage", Map.of("duration_seconds", 0, "rounds", 7,
                "tokens", 0, "tool_calls", 3));
        return Map.of("envelope", envelope("investigation.completed"), "payload", payload);
    }

    @Test
    @DisplayName("schema 真的从 classpath 加载到了，不是空壳")
    void schemasAreLoaded() {
        assertThat(ContractValidator.getInstance().schemaCount()).isEqualTo(4);
    }

    @Test
    @DisplayName("Python 真实产出的 completed 事件能通过校验")
    void acceptsRealAgentRuntimeEvent() {
        assertThatCode(() -> ContractValidator.requireValid(
                "investigation.completed", completedMessage()))
                .doesNotThrowAnyException();
    }

    @Test
    @DisplayName("枚举外的值会被拒绝，并指出是哪个字段")
    void rejectsValueOutsideEnum() {
        Map<String, Object> broken = new LinkedHashMap<>(completedMessage());
        Map<String, Object> payload = new LinkedHashMap<>(
                (Map<String, Object>) broken.get("payload"));
        payload.put("diagnosis_status", "PROBABLY_FINE");
        broken.put("payload", payload);

        assertThatThrownBy(() -> ContractValidator.requireValid(
                "investigation.completed", broken))
                .isInstanceOf(ContractViolationException.class)
                .hasMessageContaining("investigation.completed")
                .satisfies(exc -> assertThat(
                        ((ContractViolationException) exc).getViolations()).isNotEmpty());
    }

    @Test
    @DisplayName("多出的字段会被拒绝（契约是 additionalProperties:false）")
    void rejectsExtraField() {
        Map<String, Object> broken = new LinkedHashMap<>(completedMessage());
        Map<String, Object> payload = new LinkedHashMap<>(
                (Map<String, Object>) broken.get("payload"));
        payload.put("surprise", "value");
        broken.put("payload", payload);

        assertThatThrownBy(() -> ContractValidator.requireValid(
                "investigation.completed", broken))
                .isInstanceOf(ContractViolationException.class);
    }

    @Test
    @DisplayName("未登记的 event_type 直接拒绝，而不是放行")
    void rejectsUnknownEventType() {
        assertThatThrownBy(() -> ContractValidator.requireValid("investigation.unknown", Map.of()))
                .isInstanceOf(ContractViolationException.class)
                .hasMessageContaining("no schema registered");
    }

    @Test
    @DisplayName("违规信息是可读的清单，不是一句 'validation failed'")
    void violationMessagesAreActionable() {
        List<String> violations = new ArrayList<>();
        try {
            ContractValidator.requireValid("investigation.failed", Map.of(
                    "envelope", envelope("investigation.failed"),
                    "payload", Map.of("investigation_run_id", "run-1")));
        } catch (ContractViolationException exc) {
            violations.addAll(exc.getViolations());
        }

        assertThat(violations).isNotEmpty();
        assertThat(violations).anyMatch(v -> v.contains("payload"));
    }
}
