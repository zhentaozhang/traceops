package dev.traceops.controlplane.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import java.time.Duration;
import java.time.Instant;
import org.junit.jupiter.api.Test;

/**
 * 处境派生规则表。
 *
 * <p>这个测试存在的理由：{@code INVESTIGATING} 在数据库里只有一个值，
 * 但实际处境有四种。此前没有人能说出它们的区别，因为区别只存在于人的脑子里；
 * 现在它是一个纯函数，可以被穷举。
 */
class IncidentStageTest {

    private static final Instant NOW = Instant.parse("2026-11-01T12:00:00Z");

    private static IncidentStage.Signals signals(IncidentStatus status, String lastOutcome,
            int attempts, Instant nextRetryAt, int step) {
        return new IncidentStage.Signals(status, lastOutcome, attempts, 3, nextRetryAt,
                step > 0 ? "collect" : null, step, NOW);
    }

    @Test
    void aResolvedIncidentIsDoneAndWaitsOnNobody() {
        IncidentStage stage = IncidentStage.derive(signals(IncidentStatus.RESOLVED, null, 0, null, 0));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.DONE);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.NOBODY);
        assertThat(stage.retryable()).isFalse();
    }

    @Test
    void aNewIncidentWaitsForAWorkerToClaimIt() {
        IncidentStage stage = IncidentStage.derive(signals(IncidentStatus.NEW, null, 0, null, 0));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.QUEUED);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.WORKER);
        assertThat(stage.retryable()).isTrue();
    }

    @Test
    void anInvestigatingIncidentWithoutProgressIsStillQueueed() {
        IncidentStage stage = IncidentStage.derive(signals(IncidentStatus.INVESTIGATING, null, 0, null, 0));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.QUEUED);
        assertThat(stage.detail()).contains("waiting for a worker");
    }

    @Test
    void anInvestigatingIncidentWithProgressIsRunning() {
        IncidentStage stage = IncidentStage.derive(signals(IncidentStatus.INVESTIGATING, null, 0, null, 4));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.RUNNING);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.WORKER);
        assertThat(stage.detail()).contains("step 4").contains("collect");
    }

    /**
     * 这条是整项改动的理由所在。
     *
     * <p>INCONCLUSIVE 之后事故保持 INVESTIGATING（状态机里没有"诊断未完成"这个态），
     * 而重试只挂在"可重试失败"分支上 —— 于是它既不失败也不会再跑。
     * 旧视图会说"正在调查"，而实际上没有任何东西在动。
     */
    @Test
    void anInconclusiveRunIsNotRunningEvenThoughTheStatusSaysInvestigating() {
        IncidentStage stage = IncidentStage.derive(
                signals(IncidentStatus.INVESTIGATING, "NEEDS_HUMAN_REVIEW", 0, null, 6));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.NEEDS_DECISION);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.PERSON);
        assertThat(stage.retryable())
                .as("没有任何自动路径会再推进它 —— 这正是要暴露的那件事")
                .isFalse();
        assertThat(stage.detail()).contains("no automatic path");
    }

    @Test
    void aRetryInsideItsBackoffWindowWaitsOnTheClock() {
        IncidentStage stage = IncidentStage.derive(signals(IncidentStatus.INVESTIGATING, "RETRY_PENDING",
                1, NOW.plus(Duration.ofSeconds(10)), 0));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.RETRY_BACKOFF);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.CLOCK);
        assertThat(stage.detail()).contains("1/3").contains(NOW.plus(Duration.ofSeconds(10)).toString());
    }

    @Test
    void aRetryWhoseWindowHasPassedWaitsForAWorkerAgain() {
        IncidentStage stage = IncidentStage.derive(signals(IncidentStatus.INVESTIGATING, "RETRY_PENDING",
                2, NOW.minus(Duration.ofSeconds(1)), 0));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.QUEUED);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.WORKER);
        assertThat(stage.retryable()).isTrue();
    }

    /**
     * 退避边界必须是"严格晚于当前时刻才算还没到"：{@code notBefore == now} 时
     * relay 已经可以取走这一行，再显示"在等退避"就是错报。
     */
    @Test
    void aRetryDueExactlyNowIsNotStillBackingOff() {
        IncidentStage stage = IncidentStage.derive(
                signals(IncidentStatus.INVESTIGATING, "RETRY_PENDING", 2, NOW, 0));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.QUEUED);
    }

    @Test
    void anExhaustedRetryBudgetIsDeadRatherThanWaiting() {
        IncidentStage stage = IncidentStage.derive(signals(IncidentStatus.INVESTIGATING, "RETRY_PENDING",
                3, null, 0));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.DEAD);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.NOBODY);
        assertThat(stage.detail()).contains("retry budget exhausted");
        assertThat(stage.retryable()).isFalse();
    }

    @Test
    void aFailedIncidentHasNoAutomaticPathLeft() {
        IncidentStage stage = IncidentStage.derive(signals(IncidentStatus.FAILED, null, 0, null, 0));

        assertThat(stage.kind()).isEqualTo(IncidentStage.Kind.DEAD);
        assertThat(stage.waitingOn()).isEqualTo(IncidentStage.WaitingOn.NOBODY);
        assertThat(stage.detail()).contains("no automatic retry");
        assertThat(stage.retryable()).isFalse();
    }

    @Test
    void aFailedIncidentThatUsedItsWholeBudgetSaysSo() {
        IncidentStage stage = IncidentStage.derive(signals(IncidentStatus.FAILED, null, 3, null, 0));

        assertThat(stage.detail()).contains("retry budget exhausted (3/3)");
    }

    /**
     * DIAGNOSED / AWAITING_APPROVAL / REMEDIATING 都在白名单里有出边，
     * 但没有任何生产代码发出它们。派生的处境要把这件事直接说出来。
     */
    @Test
    void statesWhoseOutgoingEdgesNobodyEmitsNeedAPerson() {
        for (IncidentStatus status : new IncidentStatus[] {
                IncidentStatus.DIAGNOSED, IncidentStatus.AWAITING_APPROVAL, IncidentStatus.REMEDIATING}) {
            IncidentStage stage = IncidentStage.derive(signals(status, null, 0, null, 0));

            assertThat(stage.kind()).as(status.name()).isEqualTo(IncidentStage.Kind.NEEDS_DECISION);
            assertThat(stage.waitingOn()).as(status.name()).isEqualTo(IncidentStage.WaitingOn.PERSON);
            assertThat(stage.detail()).as(status.name()).contains("no automatic transition leaves");
        }
    }

    /** 同一个状态值必须能派生出不同处境 —— 否则这个视图就没有存在的必要。 */
    @Test
    void oneStatusCanCarryFourDifferentSituations() {
        IncidentStage queued = IncidentStage.derive(signals(IncidentStatus.INVESTIGATING, null, 0, null, 0));
        IncidentStage running = IncidentStage.derive(signals(IncidentStatus.INVESTIGATING, null, 0, null, 2));
        IncidentStage backingOff = IncidentStage.derive(
                signals(IncidentStatus.INVESTIGATING, "RETRY_PENDING", 1, NOW.plusSeconds(30), 0));
        IncidentStage needsPerson = IncidentStage.derive(
                signals(IncidentStatus.INVESTIGATING, "NEEDS_HUMAN_REVIEW", 1, null, 9));

        assertThat(queued.kind()).isNotEqualTo(running.kind());
        assertThat(running.kind()).isNotEqualTo(backingOff.kind());
        assertThat(backingOff.kind()).isNotEqualTo(needsPerson.kind());
        assertThat(java.util.Set.of(queued.kind(), running.kind(), backingOff.kind(), needsPerson.kind()))
                .hasSize(4);
    }

    @Test
    void signalsRejectNonsense() {
        assertThatThrownBy(() -> new IncidentStage.Signals(null, null, 0, 3, null, null, 0, NOW))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("status");
        assertThatThrownBy(() -> new IncidentStage.Signals(IncidentStatus.NEW, null, 0, 3, null, null, 0, null))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("now");
        assertThatThrownBy(() -> new IncidentStage.Signals(IncidentStatus.NEW, null, -1, 3, null, null, 0, NOW))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("retryAttempts");
        assertThatThrownBy(() -> new IncidentStage.Signals(IncidentStatus.NEW, null, 0, 3, null, null, -2, NOW))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("progressStep");
    }
}
