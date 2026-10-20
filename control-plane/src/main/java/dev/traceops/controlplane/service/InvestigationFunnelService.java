package dev.traceops.controlplane.service;

import dev.traceops.controlplane.domain.IncidentOrigin;
import dev.traceops.controlplane.domain.IncidentRepository;
import dev.traceops.controlplane.events.EventMessages;
import dev.traceops.controlplane.investigation.Applied;
import dev.traceops.controlplane.investigation.AppliedInvestigationEventRepository;
import dev.traceops.controlplane.outbox.OutboxRepository;
import java.time.Duration;
import java.time.Instant;
import org.springframework.stereotype.Service;

/**
 * 一次告警到一次人看的漏斗。
 *
 * <p>被替换的缺陷：{@code IngestResult.processed} 只在单次 HTTP 响应里，
 * 过了这一秒就没有任何地方能回答"今天来了多少告警、变成多少次调查、
 * 其中多少次真的得出结论、多少次卡着没人看"。缺这个数会导致两个具体误判：
 * <ul>
 *   <li>告警风暴被当成"事故变多了"—— 其实是重复告警变多了；</li>
 *   <li>"调查能力不够"和"结论没人消费"混为一谈 —— 前者要加预算/工具，
 *       后者要改流程，两者的证据在漏斗的不同格子里。</li>
 * </ul>
 *
 * <p>每一格都是独立可查的，不是"上一步减下一步"推出来的：
 * 推导出的数字无法证伪，而这里每一格都能单独对账。
 */
@Service
public class InvestigationFunnelService {

    /** 默认窗口：一小时。太短看不到风暴，太长会把两天的水位混在一起。 */
    public static final Duration DEFAULT_WINDOW = Duration.ofHours(1);

    private final IncidentRepository incidents;
    private final OutboxRepository outbox;
    private final AppliedInvestigationEventRepository appliedEvents;

    public InvestigationFunnelService(
            IncidentRepository incidents,
            OutboxRepository outbox,
            AppliedInvestigationEventRepository appliedEvents) {
        this.incidents = incidents;
        this.outbox = outbox;
        this.appliedEvents = appliedEvents;
    }

    /**
     * 漏斗快照。
     *
     * @param since 窗口起点（不含）
     */
    public Funnel snapshot(Instant since) {
        if (since == null) {
            throw new IllegalArgumentException("window start is required");
        }
        Instant checkedAt = Instant.now();
        if (since.isAfter(checkedAt)) {
            throw new IllegalArgumentException("window start must not be in the future: " + since);
        }

        long alertIncidents = incidents.countByCreatedAtAfterAndOrigin(since, IncidentOrigin.ALERT);
        long manualIncidents = incidents.countByCreatedAtAfterAndOrigin(since, IncidentOrigin.MANUAL);
        long foldedDuplicates = incidents.sumDuplicateAlertsCreatedAfter(since);

        long runsRequested =
                outbox.countByCreatedAtAfterAndEventType(since, EventMessages.INVESTIGATION_REQUESTED);
        long runsCompleted =
                appliedEvents.countByAppliedAtAfterAndEventType(since, EventMessages.INVESTIGATION_COMPLETED);
        long runsFailed =
                appliedEvents.countByAppliedAtAfterAndEventType(since, EventMessages.INVESTIGATION_FAILED);
        long needsHumanReview =
                appliedEvents.countByAppliedAtAfterAndOutcome(since, Applied.Outcome.NEEDS_HUMAN_REVIEW.name());

        return new Funnel(
                since,
                checkedAt,
                // 走到判定里的告警条数：新建的 + 被折叠掉的。这是漏斗的第一格。
                alertIncidents + foldedDuplicates,
                alertIncidents,
                manualIncidents,
                foldedDuplicates,
                runsRequested,
                runsCompleted,
                runsFailed,
                // 请求了但既没完成也没失败：还在跑，或者已经丢了。长期非零就是丢。
                Math.max(0, runsRequested - runsCompleted - runsFailed),
                needsHumanReview);
    }

    /**
     * 漏斗读数。
     *
     * @param alertsSeen        这一窗走到判定里的告警条数（新建事故 + 被折叠的重复）
     * @param alertIncidents    新建的告警事故数（= 去重后真正要调查的条数）
     * @param manualIncidents   人工创建的事故数
     * @param foldedDuplicates  命中去重、被折叠掉的告警数
     * @param runsRequested     这一窗发出的调查请求数
     * @param runsCompleted     这一窗应用完成的调查结果数
     * @param runsFailed        这一窗应用失败的调查结果数
     * @param runsWithoutResult 请求了但没有结果（在跑，或者丢了）
     * @param needsHumanReview  这一窗"跑完了但不敢下结论"的次数 —— 需要人看
     */
    public record Funnel(
            Instant since,
            Instant checkedAt,
            long alertsSeen,
            long alertIncidents,
            long manualIncidents,
            long foldedDuplicates,
            long runsRequested,
            long runsCompleted,
            long runsFailed,
            long runsWithoutResult,
            long needsHumanReview) {

        /** 被折叠掉的比例：接近 1 说明这一窗基本是重复告警，不该按事故数扩容。 */
        public double duplicateShare() {
            return alertsSeen == 0 ? 0.0 : (double) foldedDuplicates / alertsSeen;
        }
    }
}
