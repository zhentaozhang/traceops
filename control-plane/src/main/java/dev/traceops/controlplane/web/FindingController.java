package dev.traceops.controlplane.web;

import dev.traceops.controlplane.investigation.InvestigationFindingRepository;
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

    public FindingController(InvestigationFindingRepository findings) {
        this.findings = findings;
    }

    /** 404 表示"还没有结论"，不是"事故不存在" —— 调用方应先查事故再查结论。 */
    @GetMapping("/{id}/finding")
    public FindingResponse getFinding(@PathVariable String id) {
        return findings.findById(id)
                .map(FindingResponse::from)
                .orElseThrow(() -> new NoSuchElementException(
                        "no investigation finding for incident: " + id));
    }
}
