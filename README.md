# TraceOps

Evidence-driven Agentic RCA Platform —— 面向微服务系统的智能故障调查与根因分析平台。

> 一次故障从告警到诊断：Agent 分阶段调查 → 证据链可追溯 → 根因假设经过独立验证 → 修复建议经权限审批后受控执行。

## 文档导航

| 文档 | 内容 | 状态 |
|---|---|---|







## 仓库结构

```
traceops/

├── contracts/           # Java↔Python 版本化事件契约（JSON Schema 单一事实来源）
│   ├── common/          #   统一事件信封、时间窗口
│   ├── events/v1/       #   investigation.requested/updated/completed/failed
│   └── tests/           #   Python 侧契约测试（CI 种子）
├── control-plane/       # Java · Spring Boot（Incident/Alert/Service/Approval/Audit）
├── agent-runtime/       # Python · 证据引擎 + 规则 RCA + 预算 + 工具网关（TDD, 72 tests）
├── evaluation/          # 数据集 / 故障场景 / Runner / 指标 / 报告
└── demo-environment/    # 观测栈 + Fault Controller（TicketFlow 靶场后接）
```

## 技术基线（1.1 定稿）

- **Control Plane**: Spring Boot + PostgreSQL（业务事实来源）
- **Agent Runtime**: Python + LangGraph（Checkpoint 恢复）+ Budget Manager（单次 Run 上限 100k tokens）
- **协作**: 事务性 Outbox + Redis Streams；事件契约见 [contracts/](contracts/README.md)
- **可观测栈**: OTel Collector + Prometheus + Loki + Tempo + Grafana MCP（只读）
- **评估**: E1–E5 消融实验 × F-001~F-005（含级联故障 F-005）× 125 次正式运行

## 开发顺序

```
S1–S5 技术验证（TECHNICAL_SPIKES.md）
  → PRD / DOMAIN_MODEL
    → P0: TicketFlow 接入 OTel → 端到端 Alert→Evidence→RCA Report（F-001）
      → P1: Evidence Engine + RCAEval 基准 + 消融实验
        → P2: Outbox/Streams/Checkpoint 工程可靠性
          → P3: 审批 + 受控修复
```

原则：**先证明系统能正确诊断一个真实故障，再扩展它能诊断多少种故障。**

## 参考项目

本地克隆于 `../references/`（调研结论见蓝图第 0 节）：

HolmesGPT · Aurora(+aurora-lab) · RCAEval · OpenRCA · mcp-grafana · k8sgpt · kagent · robusta · keep · TriageRCA
