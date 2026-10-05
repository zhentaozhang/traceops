package dev.traceops.controlplane.web;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

/**
 * 必须有**存活**与**就绪**两个不同语义的健康端点。
 *
 * <p>缺陷：控制面此前没有任何健康端点（`/actuator/health` 实测 404）。
 *
 * <p>为什么这不是"锦上添花"：在容器编排里，
 *
 * <ul>
 *   <li><b>liveness</b> 回答"进程还活着吗" —— 失败则重启容器。
 *       它必须**不依赖外部系统**：数据库挂了不代表进程该被杀掉，
 *       那只会让故障变成"反复重启"。</li>
 *   <li><b>readiness</b> 回答"现在能接流量吗" —— 失败则摘除流量但不重启。
 *       它**可以**检查依赖（Redis/DB），因为这些依赖恢复后进程能自动变健康。</li>
 * </ul>
 *
 * <p>把两者混成一个端点是常见错误：依赖抖动会让容器被反复重启，
 * 而重启解决不了依赖问题 —— 只是把"服务慢"升级成"服务反复不可用"。
 */
@SpringBootTest
@AutoConfigureMockMvc
class HealthEndpointTest {

    @Autowired
    private MockMvc mvc;

    @Test
    void livenessDoesNotDependOnExternalSystems() throws Exception {
        // liveness 只报告"进程能响应"，不探 Redis/DB
        mvc.perform(get("/api/v1/health/live"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("UP"));
    }

    @Test
    void readinessReportsItsDependencies() throws Exception {
        mvc.perform(get("/api/v1/health/ready"))
                .andExpect(jsonPath("$.checks").isMap());
    }

    @Test
    void readinessDistinguishesDownFromAlive() throws Exception {
        // 两个端点语义必须不同：把 liveness 失败当作"该重启"、
        // readiness 失败当作"先别给流量"，是编排系统的既定行为。
        String live = mvc.perform(get("/api/v1/health/live"))
                .andReturn().getResponse().getContentAsString();
        String ready = mvc.perform(get("/api/v1/health/ready"))
                .andReturn().getResponse().getContentAsString();
        org.assertj.core.api.Assertions.assertThat(live).isNotEqualTo(ready);
    }
}
