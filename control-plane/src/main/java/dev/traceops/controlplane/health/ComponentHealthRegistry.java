package dev.traceops.controlplane.health;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Component;

/**
 * 组件健康登记的**唯一写入口**。
 *
 * <p>这个类只解决一件事：让"某条健康信息到底是谁在写"成为可验证的事实。
 *
 * <p>为什么需要它：健康条目最常见的坏法不是"没写"，而是**两个人写同一个条目**。
 * 后写的那次会覆盖前一次，于是"A 一直失败"被 B 的一次成功悄悄抹掉，
 * 面板上永远显示正常。这类故障在事后复盘时几乎无法定位 —— 因为失败的那个写入者
 * 从来没留下痕迹，它只是被覆盖了。
 *
 * <p>所以规则是：**先声明归属（{@link #claim}），才能上报（{@link #report}）**；
 * 同一个名字被第二个归属者声明时直接抛异常，而不是静默接管。
 * 这条规则和仓库里"单一写者"的其他实例（{@code IncidentWriter}、
 * {@code RunLedger.claim} 的 rowcount 仲裁）是同一个思路。
 *
 * <p>不持久化：健康是"当下"的判断，落库只会得到一份过期的真相。
 */
@Component
public class ComponentHealthRegistry {

    /** 健康等级。只用三档：再多就会退化成"自评分数"，而运维要的是能不能据此行动。 */
    public enum Severity {
        OK,
        DEGRADED,
        DOWN
    }

    /**
     * 一条健康读数。
     *
     * @param severity   等级
     * @param detail     人可读的一句话（要能直接贴进面板，例如"N pending since T"）
     * @param observedAt 观测时刻
     */
    public record Reading(Severity severity, String detail, Instant observedAt) {

        public Reading {
            if (severity == null) {
                throw new IllegalArgumentException("severity is required");
            }
            if (observedAt == null) {
                throw new IllegalArgumentException("observedAt is required");
            }
            detail = detail == null ? "" : detail;
        }
    }

    private final Map<String, String> owners = new ConcurrentHashMap<>();
    private final Map<String, Reading> readings = new ConcurrentHashMap<>();

    /**
     * 声明某个健康条目的归属。
     *
     * @throws IllegalStateException 该条目已被别的归属者声明
     */
    public void claim(String name, String owner) {
        requireText(name, "health entry name");
        requireText(owner, "health entry owner");
        String existing = owners.putIfAbsent(name, owner);
        if (existing != null && !existing.equals(owner)) {
            throw new IllegalStateException(
                    "health entry '" + name + "' is already claimed by '" + existing
                            + "', refusing claim from '" + owner
                            + "'; two writers would make the entry meaningless (the later one"
                            + " silently erases the earlier one's failure)");
        }
    }

    /** 交还归属；只有当前归属者能交还。 */
    public void release(String name, String owner) {
        requireText(name, "health entry name");
        requireText(owner, "health entry owner");
        if (owners.remove(name, owner)) {
            readings.remove(name);
        }
    }

    /**
     * 上报读数。
     *
     * @throws IllegalArgumentException 条目尚未被声明
     * @throws IllegalStateException    从非归属者上报
     */
    public void report(String name, String owner, Severity severity, String detail, Instant now) {
        requireText(name, "health entry name");
        requireText(owner, "health entry owner");
        String claimedBy = owners.get(name);
        if (claimedBy == null) {
            throw new IllegalArgumentException(
                    "health entry '" + name + "' has no claimed owner; claim it before reporting"
                            + " (an unclaimed entry has no single source of truth)");
        }
        if (!claimedBy.equals(owner)) {
            throw new IllegalStateException(
                    "health entry '" + name + "' is owned by '" + claimedBy + "', not '" + owner + "'");
        }
        readings.put(name, new Reading(severity, detail, now == null ? Instant.now() : now));
    }

    public Optional<Reading> reading(String name) {
        return Optional.ofNullable(readings.get(name));
    }

    /** 当前已声明但从未上报的条目 —— 声明了就得上报，否则那条声明是空的。 */
    public java.util.List<String> claimedButSilent() {
        return owners.keySet().stream().filter(name -> !readings.containsKey(name)).sorted().toList();
    }

    /**
     * 快照：逐条读数 + 最差等级。
     *
     * <p>{@code worst} 是给"一眼看整体"用的：只要有一条 DOWN，整体就是 DOWN；
     * 否则有 DEGRADED 就是 DEGRADED。降级不能被平均值掩盖。
     */
    public Map<String, Object> snapshot() {
        Map<String, Object> components = new LinkedHashMap<>();
        Severity worst = Severity.OK;
        for (Map.Entry<String, String> entry : owners.entrySet()) {
            String name = entry.getKey();
            Reading reading = readings.get(name);
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("owner", entry.getValue());
            if (reading == null) {
                item.put("severity", "UNKNOWN");
                item.put("detail", "claimed but never reported");
            } else {
                item.put("severity", reading.severity().name());
                item.put("detail", reading.detail());
                item.put("observedAt", reading.observedAt().toString());
                worst = worse(worst, reading.severity());
            }
            components.put(name, item);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("worst", worst.name());
        body.put("components", components);
        return body;
    }

    private static Severity worse(Severity left, Severity right) {
        if (left == Severity.DOWN || right == Severity.DOWN) {
            return Severity.DOWN;
        }
        if (left == Severity.DEGRADED || right == Severity.DEGRADED) {
            return Severity.DEGRADED;
        }
        return Severity.OK;
    }

    private static void requireText(String value, String what) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException(what + " is required");
        }
    }
}
