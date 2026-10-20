package dev.traceops.controlplane.investigation;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentStage;
import java.time.Instant;
import java.util.Optional;
import org.springframework.stereotype.Service;

/**
 * 把散落在三处的信号收拢成一次 {@link IncidentStage} 派生。
 *
 * <p>三处分别是：事故聚合（状态、重试次数、当前 run_id、重试排期）、
 * 实时进度存储（当前 run 跑到第几步）、已结算事件账本（最近一轮到底是怎么结束的）。
 *
 * <p>为什么要有这个服务而不是让 controller 自己拼：拼装逻辑一旦分散在 controller 里，
 * 每个新端点都会长出自己的一份版本，而"某个端点显示的处境和另一个不一样"
 * 是这类派生视图最常见的坏法。这里只有一个拼装点。
 */
@Service
public class IncidentStageService {

    private final InvestigationProgressStore progress;
    private final AppliedInvestigationEventRepository appliedEvents;
    private final InvestigationRetryService retries;

    public IncidentStageService(InvestigationProgressStore progress,
            AppliedInvestigationEventRepository appliedEvents,
            InvestigationRetryService retries) {
        this.progress = progress;
        this.appliedEvents = appliedEvents;
        this.retries = retries;
    }

    /** 以当前时刻为基准派生处境。 */
    public IncidentStage describe(Incident incident) {
        return describe(incident, Instant.now());
    }

    /**
     * 派生处境（显式基准时刻，便于测试覆盖退避窗口边界）。
     *
     * <p>当前 run 的进度只在 run_id 存在时查：没有 run_id 就是"还没有过调查"，
     * 而不是"有一个跑到第 0 步的调查"。
     */
    public IncidentStage describe(Incident incident, Instant now) {
        if (incident == null) {
            throw new IllegalArgumentException("incident is required");
        }
        String runId = incident.getCurrentRunId();
        Optional<InvestigationProgress> latestProgress = runId == null
                ? Optional.empty()
                : progress.latest(runId);

        // 最近一轮的结算结论：只有在拿得到当前 run 时才查，避免用一条旧 run 的结论
        // 去描述一个刚发起、还没结算的新 run。
        String lastOutcome = runId == null
                ? null
                : appliedEvents.findByInvestigationRunId(runId)
                        .map(AppliedInvestigationEvent::getOutcome)
                        .orElse(null);

        IncidentStage.Signals signals = new IncidentStage.Signals(
                incident.getStatus(),
                lastOutcome,
                incident.getRetryAttempts(),
                retries.maxAttempts(),
                incident.getNextRetryAt(),
                latestProgress.map(InvestigationProgress::currentNode).orElse(null),
                latestProgress.map(InvestigationProgress::stepIndex).orElse(0),
                now);
        return IncidentStage.derive(signals);
    }
}
