package dev.traceops.controlplane.web;

import dev.traceops.controlplane.investigation.FindingHistoryRepository;
import dev.traceops.controlplane.investigation.InvestigationFindingRepository;
import java.util.List;
import java.util.NoSuchElementException;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 诊断结论查询。
 *
 * <p>路径挂在 incident 下而不是独立资源：结论的生命周期从属于事故，
 * 事故关闭后仍要能回看结论（审计要求），所以不做级联删除语义。
 */
@RestController
@RequestMapping("/api/v1/incidents")
public class FindingController {

    private final InvestigationFindingRepository findings;
    private final FindingHistoryRepository history;

    public FindingController(InvestigationFindingRepository findings,
                             FindingHistoryRepository history) {
        this.findings = findings;
        this.history = history;
    }

    /** 404 表示"还没有结论"，不是"事故不存在" —— 调用方应先查事故再查结论。 */
    @GetMapping("/{id}/finding")
    public FindingResponse getFinding(@PathVariable String id) {
        return findings.findById(id)
                .map(FindingResponse::from)
                .orElseThrow(() -> new NoSuchElementException(
                        "no investigation finding for incident: " + id));
    }

    /**
     * 该事故的**历次**调查结论（按时间正序）。
     *
     * <p>为什么与 {@code /finding} 并存而不是替换它：查"当前结论"是最热的路径
     * （SRE 打开事故详情就调它），不该为了多轮对比把它变成一次列表查询。
     *
     * <p>没有历史时返回空数组而不是 404：事故存在但没被调查过是**正常状态**，
     * 而 404 会被调用方误读成"事故不存在"。
     */
    @GetMapping("/{id}/findings")
    public List<FindingHistorySnapshotResponse> listHistory(@PathVariable String id) {
        return history.findByIncidentIdOrderByRecordedAtAsc(id).stream()
                .map(FindingHistorySnapshotResponse::from)
                .toList();
    }
}
