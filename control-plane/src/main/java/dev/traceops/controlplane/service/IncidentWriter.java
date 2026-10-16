package dev.traceops.controlplane.service;

import dev.traceops.controlplane.domain.Incident;
import dev.traceops.controlplane.domain.IncidentRepository;
import org.springframework.stereotype.Component;

/**
 * 事故的唯一写者。
 *
 * <h2>为什么要有这一层</h2>
 *
 * <p>改之前，{@code incidents.save(...)} 在 6 个地方被调用，分布在两个服务里：
 *
 * <ul>
 *   <li>{@code IncidentService} 4 处（入库、人工创建、关联后更新、转状态）</li>
 *   <li>{@code InvestigationResultService} 2 处（应用结果、排重试）</li>
 * </ul>
 *
 * <p>这本身不算错 —— 两处都是合法的写入时机。问题在于它让"事故怎样才算被改过"
 * 没有单一答案：想加一条"每次状态变化都要写审计"，就得去 6 个地方各加一遍，
 * 而**漏掉的那一处不会报错**。上一轮审计发现"第三次结论变更没有进统一审计中心"
 * 就是这么来的：写入口不止一个，覆盖就靠记性。
 *
 * <p>所以把写入口收到这里。今天的实现对每个调用方都是等价的
 * （{@link #save} 就是一次 {@code repository.save}），但以后任何"改事故时必须同时做
 * 某事"的要求，只有一处需要改。这是这一层唯一的价值，也是它唯一的职责 ——
 * 它不接管状态机、不接管事务边界，那些仍归调用方。
 *
 * <p>约束由 {@code IncidentWriteSeamTest} 守住：除本类之外，生产代码里
 * 不允许出现对 {@code IncidentRepository#save} 的调用。
 */
@Component
public class IncidentWriter {

    private final IncidentRepository incidents;

    public IncidentWriter(IncidentRepository incidents) {
        this.incidents = incidents;
    }

    /**
     * 持久化一条事故。
     *
     * <p>刻意不做"是否脏"的判断：`Incident` 是 JPA 实体，调用方改完字段后
     * 这里就是提交点。加上脏检查会让"我改了但没保存"变成静默失败。
     */
    public Incident save(Incident incident) {
        if (incident == null) {
            throw new IllegalArgumentException("incident must not be null");
        }
        return incidents.save(incident);
    }
}
