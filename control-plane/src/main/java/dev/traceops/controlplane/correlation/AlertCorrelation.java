package dev.traceops.controlplane.correlation;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * 告警关联（蓝图 §2.1）。
 *
 * <p>核心立场：<b>告警不等于事故</b>。同一个故障会从多个出口冒出来
 * （500 比例、Redis 超时、P99 延迟），如果每条都开一次 Agent 调查，
 * 会产生重复查询和互相矛盾的结论。
 *
 * <p>关联必须是<b>确定性规则</b>：环境相同 + 时间窗重叠 + 服务相同或存在依赖。
 * 不允许 LLM 参与基础关联 —— 它无法解释"为什么这两条告警被合并"。
 */
public final class AlertCorrelation {

    private final Duration window;
    private final ServiceDependencyLookup dependencies;

    public AlertCorrelation(Duration window, ServiceDependencyLookup dependencies) {
        if (window.isNegative() || window.isZero()) {
            throw new IllegalArgumentException("correlation window must be positive");
        }
        this.window = window;
        this.dependencies = dependencies;
    }

    public record Candidate(String fingerprint, String environment, String service, Instant startsAt) {
        public Candidate {
            if (fingerprint == null || fingerprint.isBlank()) {
                throw new IllegalArgumentException("fingerprint required");
            }
            if (service == null || service.isBlank()) {
                throw new IllegalArgumentException("service required");
            }
        }
    }

    public enum Reason {
        SAME_FINGERPRINT,
        SAME_SERVICE_WITHIN_WINDOW,
        DEPENDENT_SERVICE_WITHIN_WINDOW,
        UNRELATED
    }

    public record Verdict(boolean correlated, Reason reason, String explanation) {
    }

    /**
     * 判断 incoming 是否应并入 existing 所在的事故。
     *
     * <p>顺序：指纹完全相同 → 直接合并（最强信号）；
     * 其次看环境 + 时间窗 + 服务/依赖关系。
     */
    public Verdict shouldCorrelate(Candidate existing, Candidate incoming) {
        if (existing.fingerprint().equals(incoming.fingerprint())) {
            return new Verdict(true, Reason.SAME_FINGERPRINT,
                    "identical fingerprint: " + incoming.fingerprint());
        }
        if (!existing.environment().equals(incoming.environment())) {
            return new Verdict(false, Reason.UNRELATED,
                    "different environments: " + existing.environment() + " vs " + incoming.environment());
        }

        long gapSeconds = Math.abs(Duration.between(existing.startsAt(), incoming.startsAt()).getSeconds());
        if (gapSeconds > window.getSeconds()) {
            return new Verdict(false, Reason.UNRELATED,
                    "outside correlation window: " + gapSeconds + "s > " + window.getSeconds() + "s");
        }

        if (existing.service().equals(incoming.service())) {
            return new Verdict(true, Reason.SAME_SERVICE_WITHIN_WINDOW,
                    "same service " + existing.service() + " within " + gapSeconds + "s");
        }

        if (dependencies.related(existing.service(), incoming.service())) {
            return new Verdict(true, Reason.DEPENDENT_SERVICE_WITHIN_WINDOW,
                    "dependent services " + existing.service() + " <-> " + incoming.service()
                            + " within " + gapSeconds + "s");
        }

        return new Verdict(false, Reason.UNRELATED,
                "services unrelated: " + existing.service() + " / " + incoming.service());
    }

    /**
     * 把一个 alarm 批次聚成事故组。
     *
     * <p>实现是增量合并（union-find 的朴素版）：新告警并入第一个匹配的组，
     * 保证同一输入永远得到同样的分组顺序。
     */
    public List<List<Candidate>> group(List<Candidate> alerts) {
        List<List<Candidate>> groups = new ArrayList<>();
        for (Candidate alert : alerts) {
            boolean placed = false;
            for (List<Candidate> group : groups) {
                // 只要与组内任一条相关就并入（传递关联）
                for (Candidate member : group) {
                    if (shouldCorrelate(member, alert).correlated()) {
                        group.add(alert);
                        placed = true;
                        break;
                    }
                }
                if (placed) {
                    break;
                }
            }
            if (!placed) {
                List<Candidate> fresh = new ArrayList<>();
                fresh.add(alert);
                groups.add(fresh);
            }
        }
        return groups;
    }

    public interface ServiceDependencyLookup {
        boolean related(String first, String second);
    }

    /** 配置文件声明的静态依赖（蓝图 §2.7：第一版不做自动发现）。 */
    public static ServiceDependencyLookup staticTopology(Set<List<String>> edges) {
        Set<String> normalized = new HashSet<>();
        for (List<String> edge : edges) {
            if (edge.size() != 2) {
                throw new IllegalArgumentException("edge must be [caller, callee]");
            }
            String a = edge.get(0);
            String b = edge.get(1);
            normalized.add(a + "->" + b);
            normalized.add(b + "->" + a);
        }
        return (first, second) -> normalized.contains(first + "->" + second);
    }
}
