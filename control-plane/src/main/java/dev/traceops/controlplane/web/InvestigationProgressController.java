package dev.traceops.controlplane.web;

import dev.traceops.controlplane.investigation.InvestigationProgress;
import dev.traceops.controlplane.investigation.InvestigationProgressStore;
import java.io.IOException;
import java.time.Duration;
import java.util.Optional;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * 调查进度的实时推送（SSE）。
 *
 * <p>两条端点，语义不同：
 * <ul>
 *   <li>{@code GET /{runId}/progress} —— 当前进度的**快照**（普通 JSON）。
 *       客户端刚连上时先拿一份，避免空等第一个事件。</li>
 *   <li>{@code GET /{runId}/progress/stream} —— SSE 流，持续推送后续进度。</li>
 * </ul>
 *
 * <p>为什么快照端点必须存在：SSE 只推**后续**变化。若客户端在调查中途连上，
 * 只订阅流会看到空白，直到下一个节点开始 —— 那段时间里"什么都没有"和
 * "调查没启动"无法区分。
 *
 * <p>超时：SSE 连接不能无限挂着（会耗尽容器线程）。这里给 30 分钟上限，
 * 与一次调查的最长合理时长对齐；超时后客户端应重连并重新拉快照。
 */
@RestController
@RequestMapping("/api/v1/investigations")
public class InvestigationProgressController {

    /** 与"一次调查最长合理时长"对齐；超时后客户端重连即可。 */
    private static final Duration STREAM_TIMEOUT = Duration.ofMinutes(30);

    private final InvestigationProgressStore progressStore;

    public InvestigationProgressController(InvestigationProgressStore progressStore) {
        this.progressStore = progressStore;
    }

    /** 当前进度快照。没有进度返回 404 —— 404 表示"没有正在进行的进度"。 */
    @GetMapping("/{runId}/progress")
    public InvestigationProgress getProgress(@PathVariable String runId) {
        Optional<InvestigationProgress> latest = progressStore.latest(runId);
        if (latest.isEmpty()) {
            throw new java.util.NoSuchElementException(
                    "no progress for investigation run: " + runId);
        }
        return latest.get();
    }

    /**
     * 进度流。连接建立后**先推当前快照**（若有），再推后续变更。
     *
     * <p>先推快照是刻意的：否则中途接入的客户端要等到下一个节点才知道进度，
     * 而"等待"与"根本没有调查"在客户端看来是一样的。
     */
    @GetMapping(value = "/{runId}/progress/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter streamProgress(@PathVariable String runId) {
        SseEmitter emitter = new SseEmitter(STREAM_TIMEOUT.toMillis());

        // 先发当前快照
        progressStore.latest(runId).ifPresent(progress -> send(emitter, runId, progress));

        Runnable unsubscribe = progressStore.subscribe(runId, progress -> send(emitter, runId, progress));
        emitter.onCompletion(unsubscribe);
        emitter.onTimeout(() -> {
            unsubscribe.run();
            emitter.complete();
        });
        emitter.onError(error -> unsubscribe.run());
        return emitter;
    }

    private void send(SseEmitter emitter, String runId, InvestigationProgress progress) {
        try {
            emitter.send(SseEmitter.event()
                    .id(runId + "-" + progress.stepIndex())
                    .name("progress")
                    .data(progress, MediaType.APPLICATION_JSON));
        } catch (IOException | IllegalStateException ex) {
            // 客户端断开是正常情况，不是错误：不再推送即可
            emitter.completeWithError(ex);
        }
    }
}
