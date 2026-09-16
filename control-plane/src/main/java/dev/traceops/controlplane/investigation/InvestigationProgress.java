package dev.traceops.controlplane.investigation;

/**
 * 一个调查 run 的当前进度（来自 {@code investigation.updated}）。
 *
 * <p>这是**状态**而不是流水：同一 run 的新进度覆盖旧的。
 * 历史进度属于审计中心的职责，不该混在实时视图里。
 *
 * @param runId            调查 run 标识
 * @param currentNode      当前节点名（plan/collect/...）
 * @param stepIndex        单调递增的步骤序号，用于 SSE 客户端重排
 * @param stepDescription  人可读描述
 * @param evidenceCount    该节点完成时已收集的证据数
 */
public record InvestigationProgress(
        String runId,
        String currentNode,
        int stepIndex,
        String stepDescription,
        int evidenceCount) {
}
