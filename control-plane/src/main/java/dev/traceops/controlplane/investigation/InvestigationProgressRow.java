package dev.traceops.controlplane.investigation;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;

/**
 * 进度的持久化形态。
 *
 * <p>为什么不只放内存：控制面重启时 worker 还在跑（它有自己的 900 秒 deadline），
 * 重启后内存里的进度全空，事故停在 {@code INVESTIGATING}、派生的 stage 退回
 * QUEUED —— 于是"正在调查"这件事在控制面这边看起来像"排队中"，
 * 而 worker 那边其实已经在第三步了。两端对同一次调查各说各话。
 *
 * <p>表只有一行的宽度、按 run_id 覆盖写，所以它同时是"读穿缓存的后备"和
 * "重启后唯一的记忆"。清理仍然由 {@link InvestigationProgressStore} 的 TTL 负责。
 */
@Entity
@Table(name = "investigation_progress")
public class InvestigationProgressRow {

    @Id
    private String runId;

    private String currentNode;

    @Column(nullable = false)
    private int stepIndex;

    @Column(length = 1000)
    private String stepDescription;

    @Column(nullable = false)
    private int evidenceCount;

    @Column(nullable = false)
    private Instant updatedAt;

    protected InvestigationProgressRow() {
    }

    private InvestigationProgressRow(String runId, InvestigationProgress progress, Instant updatedAt) {
        this.runId = runId;
        this.currentNode = progress.currentNode();
        this.stepIndex = progress.stepIndex();
        this.stepDescription = progress.stepDescription();
        this.evidenceCount = progress.evidenceCount();
        this.updatedAt = updatedAt;
    }

    static InvestigationProgressRow of(String runId, InvestigationProgress progress, Instant updatedAt) {
        return new InvestigationProgressRow(runId, progress, updatedAt);
    }

    /** 用新进度覆盖自身；stepIndex 不倒退，与内存实现保持同一条语义。 */
    void overwrite(InvestigationProgress progress, Instant updatedAt) {
        this.currentNode = progress.currentNode();
        this.stepIndex = progress.stepIndex();
        this.stepDescription = progress.stepDescription();
        this.evidenceCount = progress.evidenceCount();
        this.updatedAt = updatedAt;
    }

    InvestigationProgress toProgress() {
        return new InvestigationProgress(runId, currentNode, stepIndex, stepDescription, evidenceCount);
    }

    Instant updatedAt() {
        return updatedAt;
    }

    String runId() {
        return runId;
    }
}
