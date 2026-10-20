package dev.traceops.controlplane.domain;

import java.time.Instant;

/**
 * 事故此刻"卡在谁那里"的派生视图。
 *
 * <p>为什么需要它：{@link IncidentStatus} 表达的是**流程走到哪一步**，它回答不了
 * "此刻谁该动、还要等多久"。停在 {@code INVESTIGATING} 的事故可以是四种完全不同的处境：
 * <ul>
 *   <li>请求已入 outbox，还没有 worker 认领；</li>
 *   <li>worker 正在跑；</li>
 *   <li>上一轮失败，正在等退避窗口；</li>
 *   <li>上一轮跑完但没有给出决定性结论，而这个仓库里**没有任何东西会再推进它**。</li>
 * </ul>
 * 这四种处境在数据库里是同一个状态值，运维者从接口上看不出来 ——
 * 于是"调查卡住了"永远只能靠读代码判断。这个类型把那个判断写成可测试的纯函数。
 *
 * <p>为什么派生而不是持久化：它是状态 + 时间 + 最近一条回程事件的函数，
 * 落库就会有两份事实来源，而第二份永远会落后于第一份（这正是
 * {@code InvestigationStatus} 里 {@code AWAITING_APPROVAL}/{@code REMEDIATING}
 * 变成死状态却没人发现的机制）。
 *
 * <p>规则按顺序匹配，**首个命中胜**。顺序本身是语义的一部分，见 {@link #derive}。
 *
 * @param kind      处境分类
 * @param waitingOn 此刻在等谁动
 * @param detail    人可读的一句话（要能直接贴进告警面板）
 * @param retryable 是否存在能继续推进它的自动路径
 */
public record IncidentStage(Kind kind, WaitingOn waitingOn, String detail, boolean retryable) {

    /** 处境分类。刻意只有六种：再多就会退化成状态机的第二个副本。 */
    public enum Kind {
        /** 请求已就绪，等 worker 认领。 */
        QUEUED,
        /** worker 正在跑。 */
        RUNNING,
        /** 在退避窗口里等，时间到了会自动重新投递。 */
        RETRY_BACKOFF,
        /** 需要人做决定：状态机允许往前走，但没有任何东西会自动发出那条边。 */
        NEEDS_DECISION,
        /** 已解决。 */
        DONE,
        /** 已经没有自动路径：重试预算用光，或失败不可重试。 */
        DEAD
    }

    /** 此刻在等谁。 */
    public enum WaitingOn {
        /** 等 agent worker（认领或跑完）。 */
        WORKER,
        /** 等时间（退避窗口）。 */
        CLOCK,
        /** 等人。 */
        PERSON,
        /** 谁也不等：已结束或已死。 */
        NOBODY
    }

    /**
     * 派生所需的全部信号。
     *
     * <p>刻意是扁平的原语而不是直接传 {@link Incident}：纯函数才能被穷举测试，
     * 而这类"判断此刻处境"的逻辑一旦只能靠构造整条链路来验证，就会长期没人验证。
     *
     * @param status            事故当前状态
     * @param lastOutcome       最近一条已结算回程事件的结论名（{@code Applied.Outcome}），
     *                          从未收到过回程时为 {@code null}
     * @param retryAttempts     已安排过的重试轮数
     * @param maxRetryAttempts  重试预算上限
     * @param nextRetryAt       下一轮重试最早可发出时刻；没有排期中的重试为 {@code null}
     * @param progressNode      当前 run 的最新节点名；没有进度为 {@code null}
     * @param progressStep      当前 run 的最新步骤序号；没有进度为 0
     * @param now               判定基准时刻（显式传入，测试才能覆盖时间边界）
     */
    public record Signals(
            IncidentStatus status,
            String lastOutcome,
            int retryAttempts,
            int maxRetryAttempts,
            Instant nextRetryAt,
            String progressNode,
            int progressStep,
            Instant now) {

        public Signals {
            if (status == null) {
                throw new IllegalArgumentException("status is required");
            }
            if (now == null) {
                throw new IllegalArgumentException("now is required");
            }
            if (retryAttempts < 0) {
                throw new IllegalArgumentException("retryAttempts cannot be negative: " + retryAttempts);
            }
            if (maxRetryAttempts < 0) {
                throw new IllegalArgumentException(
                        "maxRetryAttempts cannot be negative: " + maxRetryAttempts);
            }
            if (progressStep < 0) {
                throw new IllegalArgumentException("progressStep cannot be negative: " + progressStep);
            }
        }
    }

    /** 回程事件结论里表示"这一轮已经结算、但没有决定性诊断"的那几个（见 {@code Applied.Outcome}）。 */
    private static final String OUTCOME_NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW";
    private static final String OUTCOME_RETRY_PENDING = "RETRY_PENDING";

    /**
     * 派生事故处境。
     *
     * <p>顺序即语义，按此顺序首个命中胜：
     * <ol>
     *   <li><b>已解决</b>：停在这里，不再判断其它信号（一个已解决的事故还在"等重试"
     *       一定是数据不一致，把它报成 DONE 反而让不一致可见）。</li>
     *   <li><b>最近一轮没有决定性结论</b>：状态机里没有"诊断未完成"这个态，
     *       于是它停在 {@code INVESTIGATING}；此刻既没有 worker 在跑，也没有排期中的重试。
     *       必须先于"是否正在跑"判断 —— 否则会报成 RUNNING，而那正是要消除的误读。</li>
     *   <li><b>可重试失败</b>：按退避窗口、然后是重试预算，区分"还要等"和"已经用光"。</li>
     *   <li><b>失败</b>：不可重试或已判死。</li>
     *   <li><b>调查中</b>：有进度就是真在跑，没进度就是在等认领。</li>
     *   <li><b>其余</b>：需要人。</li>
     * </ol>
     */
    public static IncidentStage derive(Signals s) {
        if (s.status() == IncidentStatus.RESOLVED) {
            return new IncidentStage(Kind.DONE, WaitingOn.NOBODY, "resolved", false);
        }

        if (OUTCOME_NEEDS_HUMAN_REVIEW.equals(s.lastOutcome())) {
            return new IncidentStage(Kind.NEEDS_DECISION, WaitingOn.PERSON,
                    "last run ended without a decisive diagnosis; no automatic path advances this",
                    false);
        }

        if (OUTCOME_RETRY_PENDING.equals(s.lastOutcome())) {
            return retryBackoff(s);
        }

        if (s.status() == IncidentStatus.FAILED) {
            return new IncidentStage(Kind.DEAD, WaitingOn.NOBODY,
                    s.retryAttempts() >= s.maxRetryAttempts()
                            ? "failed: retry budget exhausted (" + s.retryAttempts() + "/"
                                    + s.maxRetryAttempts() + ")"
                            : "failed: no automatic retry applies to this failure",
                    false);
        }

        if (s.status() == IncidentStatus.NEW) {
            return new IncidentStage(Kind.QUEUED, WaitingOn.WORKER,
                    "waiting for a worker to claim the investigation", true);
        }

        if (s.status() == IncidentStatus.INVESTIGATING) {
            // 排期中的重试优先于进度判断：重试是"新的一轮"，而上一轮的进度可能还留着。
            if (s.nextRetryAt() != null) {
                return retryBackoff(s);
            }
            if (s.progressStep() > 0) {
                String node = s.progressNode() == null ? "unknown" : s.progressNode();
                return new IncidentStage(Kind.RUNNING, WaitingOn.WORKER,
                        "investigating: step " + s.progressStep() + " (" + node + ")", true);
            }
            return new IncidentStage(Kind.QUEUED, WaitingOn.WORKER,
                    "waiting for a worker to claim the investigation", true);
        }

        // DIAGNOSED / AWAITING_APPROVAL / REMEDIATING：状态机的白名单里有出边，
        // 但没有任何生产代码会发出那条边。把这句话直接写进 detail，
        // 比让人去读 ALLOWED 表之后自己得出同样结论要好。
        return new IncidentStage(Kind.NEEDS_DECISION, WaitingOn.PERSON,
                "no automatic transition leaves " + s.status() + "; a person must decide", false);
    }

    private static IncidentStage retryBackoff(Signals s) {
        if (s.retryAttempts() >= s.maxRetryAttempts() && s.nextRetryAt() == null) {
            return new IncidentStage(Kind.DEAD, WaitingOn.NOBODY,
                    "retry budget exhausted (" + s.retryAttempts() + "/" + s.maxRetryAttempts() + ")",
                    false);
        }
        if (s.nextRetryAt() == null) {
            return new IncidentStage(Kind.QUEUED, WaitingOn.WORKER,
                    "retry due but not yet claimed", true);
        }
        if (s.nextRetryAt().isAfter(s.now())) {
            return new IncidentStage(Kind.RETRY_BACKOFF, WaitingOn.CLOCK,
                    "retry " + (s.retryAttempts()) + "/" + s.maxRetryAttempts()
                            + " scheduled at " + s.nextRetryAt(),
                    true);
        }
        return new IncidentStage(Kind.QUEUED, WaitingOn.WORKER,
                "retry " + s.retryAttempts() + "/" + s.maxRetryAttempts()
                        + " is due and waiting for a worker",
                true);
    }
}
