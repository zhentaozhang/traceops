package dev.traceops.controlplane.health;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.Instant;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * 健康登记表的规则测试。
 *
 * <p>重点不是"能写能读"，而是三条**拒绝**：重复声明、未声明就上报、非归属者上报。
 * 这三条如果不生效，这个类就退化成另一个"谁都能写的全局 Map"，
 * 而它存在的唯一理由恰恰是让归属可验证。
 */
class ComponentHealthRegistryTest {

    private static final Instant NOW = Instant.parse("2026-11-01T12:00:00Z");

    @Test
    void aClaimedEntryAcceptsReadingsFromItsOwner() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();
        registry.claim("db", "writer-a");
        registry.report("db", "writer-a", ComponentHealthRegistry.Severity.OK, "fine", NOW);

        assertThat(registry.reading("db")).get()
                .extracting(ComponentHealthRegistry.Reading::severity)
                .isEqualTo(ComponentHealthRegistry.Severity.OK);
    }

    /** 同一个人重复声明是幂等的 —— 它只是"我还在写这一条"。 */
    @Test
    void theSameOwnerMayClaimTwice() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();
        registry.claim("db", "writer-a");
        registry.claim("db", "writer-a");

        assertThat(registry.claimedButSilent()).containsExactly("db");
    }

    /**
     * 第二个归属者必须被拒绝。
     *
     * <p>这是整个类存在的理由：允许接管的话，后写者的正常会把前写者的失败静默抹掉，
     * 而抹掉之后没有任何痕迹能证明前写者曾经失败过。
     */
    @Test
    void aSecondOwnerIsRefusedRatherThanAllowedToTakeOver() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();
        registry.claim("db", "writer-a");

        assertThatThrownBy(() -> registry.claim("db", "writer-b"))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("writer-a")
                .hasMessageContaining("writer-b");
    }

    @Test
    void reportingWithoutClaimingIsRefused() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();

        assertThatThrownBy(() -> registry.report(
                "ghost", "writer-a", ComponentHealthRegistry.Severity.OK, "fine", NOW))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("no claimed owner");
    }

    @Test
    void reportingFromANonOwnerIsRefused() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();
        registry.claim("db", "writer-a");

        assertThatThrownBy(() -> registry.report(
                "db", "writer-b", ComponentHealthRegistry.Severity.DOWN, "broken", NOW))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("writer-a");
    }

    @Test
    void releasingLetsAnotherOwnerClaimAndClearsTheReading() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();
        registry.claim("db", "writer-a");
        registry.report("db", "writer-a", ComponentHealthRegistry.Severity.DOWN, "broken", NOW);

        registry.release("db", "writer-a");

        assertThat(registry.reading("db")).isEmpty();
        registry.claim("db", "writer-b");
        assertThat(registry.snapshot().get("components").toString()).contains("writer-b");
    }

    @Test
    void releaseFromANonOwnerDoesNothing() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();
        registry.claim("db", "writer-a");

        registry.release("db", "writer-b");

        assertThatThrownBy(() -> registry.claim("db", "writer-c"))
                .isInstanceOf(IllegalStateException.class);
    }

    /** 声明了没上报的条目必须能被列出来，否则"加了条目忘接数据源"永远静默。 */
    @Test
    void claimedButSilentListsEntriesWithNoReading() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();
        registry.claim("db", "writer-a");
        registry.claim("redis", "writer-b");
        registry.report("redis", "writer-b", ComponentHealthRegistry.Severity.OK, "fine", NOW);

        assertThat(registry.claimedButSilent()).containsExactly("db");
        Map<String, Object> components =
                (Map<String, Object>) registry.snapshot().get("components");
        assertThat(((Map<String, Object>) components.get("db")).get("severity"))
                .isEqualTo("UNKNOWN");
    }

    /** 最差等级必须是"最差"，不能被平均值或最后一条掩盖。 */
    @Test
    void worstIsTheWorstSeverityAmongReportedEntries() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();
        registry.claim("ok", "writer-a");
        registry.claim("degraded", "writer-b");
        registry.claim("down", "writer-c");
        registry.claim("silent", "writer-d");
        registry.report("ok", "writer-a", ComponentHealthRegistry.Severity.OK, "", NOW);
        registry.report("degraded", "writer-b", ComponentHealthRegistry.Severity.DEGRADED, "", NOW);
        registry.report("down", "writer-c", ComponentHealthRegistry.Severity.DOWN, "", NOW);

        assertThat(registry.snapshot().get("worst")).isEqualTo("DOWN");
    }

    @Test
    void worstFallsBackToDegradedWhenNothingIsDown() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();
        registry.claim("ok", "writer-a");
        registry.claim("degraded", "writer-b");
        registry.report("ok", "writer-a", ComponentHealthRegistry.Severity.OK, "", NOW);
        registry.report("degraded", "writer-b", ComponentHealthRegistry.Severity.DEGRADED, "", NOW);

        assertThat(registry.snapshot().get("worst")).isEqualTo("DEGRADED");
    }

    @Test
    void blankNamesAreRefused() {
        ComponentHealthRegistry registry = new ComponentHealthRegistry();

        assertThatThrownBy(() -> registry.claim(" ", "writer-a"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> registry.claim("db", ""))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void aReadingNeedsASeverityAndATimestamp() {
        assertThatThrownBy(() -> new ComponentHealthRegistry.Reading(null, "", NOW))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new ComponentHealthRegistry.Reading(
                ComponentHealthRegistry.Severity.OK, "", null))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
