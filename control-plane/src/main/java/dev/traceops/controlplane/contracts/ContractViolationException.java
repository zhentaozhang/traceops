package dev.traceops.controlplane.contracts;

import java.util.List;

/**
 * 组装出的事件与 {@code contracts/} 里的 JSON Schema 不符。
 *
 * <p>故意继承 {@link IllegalStateException} 而不是某个受检异常：
 * 契约违规是**编程错误**，不是可以重试或可以降级的运行期状况。
 * 它应当让产生这条消息的那个请求失败，而不是被 catch 掉继续跑 ——
 * 一旦被吞掉，坏消息就进了 outbox，几秒钟后变成一条投递失败的死信，
 * 而真正出错的那一行早就从栈里消失了。
 */
public class ContractViolationException extends IllegalStateException {

    private final String eventType;
    private final List<String> violations;

    public ContractViolationException(String eventType, List<String> violations) {
        super(buildMessage(eventType, violations));
        this.eventType = eventType;
        this.violations = List.copyOf(violations);
    }

    public String getEventType() {
        return eventType;
    }

    public List<String> getViolations() {
        return violations;
    }

    private static String buildMessage(String eventType, List<String> violations) {
        StringBuilder sb = new StringBuilder("event '")
                .append(eventType)
                .append("' violates its contract (")
                .append(violations.size())
                .append(" violation(s)):");
        for (String violation : violations) {
            sb.append("\n  - ").append(violation);
        }
        return sb.toString();
    }
}
