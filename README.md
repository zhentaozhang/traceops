# TraceOps

Evidence-driven Agentic RCA Platform —— 面向微服务系统的智能故障调查与根因分析平台。

> 一次故障从告警到诊断：Agent 分阶段调查 → 证据链可追溯 → 根因假设经过独立验证 →
> 修复建议经权限审批后受控执行。

## 当前状态（P0）

| 平面 | 状态 | 测试 |
|---|---|---|
| Control Plane（Java / Spring Boot） | ✅ 告警接入、事故状态机、事务性 Outbox、契约校验 | 18 passing |
| Investigation Plane（Python / LangGraph） | ✅ 6 节点确定性调查图、预算守护 | 97 passing（含评估） |
| Evidence Plane（Python） | ✅ 4 类归一化、内容寻址校验、报告引用闸门 | 同上 |
| Evaluation Plane（Python） | ✅ 6 场景 + 指标 + runner CLI | 同上 |
| Execution Plane（受控修复） | ⬜ 未实现（P3） | — |
| 观测栈（docker compose） | ✅ 文件就绪 | 未拉起（Docker 守护进程未运行） |

**P0 没有 LLM 参与**：这一版全部是确定性代码，目的是把"证据 → 假设 → 排序 → 报告"
这条数据流先做实，再做模型。E1–E5 消融实验因此还跑不了，见 `评测口径` §4。

## 文档

| 文档 | 内容 | 状态 |
|---|---|---|








## 仓库结构

```
traceops/

├── contracts/                 # Java↔Python 版本化事件契约（JSON Schema 单一事实来源）
│   ├── common/                #   事件信封、时间窗口
│   ├── events/v1/             #   investigation.requested/updated/completed/failed
│   └── tests/                 #   Python 侧契约测试（10 passing）
├── control-plane/             # Java 21 · Spring Boot 3.5（状态机 + Outbox + Web + 契约校验）
├── agent-runtime/             # Python 3.11 · LangGraph 1.2（证据引擎 + 规则 RCA + 适配器 + 预算）
├── evaluation/                # 场景 JSON + harness + 指标 + runner CLI
└── demo-environment/
    └── observability/         # Prometheus + Loki + Tempo + Grafana（docker compose）
```

## 快速开始

### Python 侧（证据引擎 + 调查 + 评估）

```bash
cd traceops
python -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e agent-runtime --no-build-isolation

# 全部测试（97）
.venv/bin/python -m pytest agent-runtime/tests evaluation -q

# 跑评估：6 场景 + 指标
.venv/bin/python -m evaluation.runner
```

### Java 侧（Control Plane）

```bash
cd traceops/control-plane
mvn -s .mvn/settings.xml -B test        # 18 tests
mvn -s .mvn/settings.xml spring-boot:run # :8081

curl -X POST localhost:8081/api/v1/alerts -H 'Content-Type: application/json' -d '{
  "alerts":[{"fingerprint":"fp-demo","status":"firing","service":"order-service",
             "severity":"critical","summary":"redis pool exhausted"}]}'
```

> `.mvn/settings.xml` 是本机环境适配（`~/.m2` 不可写 + Maven Central 直连 403，
> 故本地仓库落工作区内、镜像到阿里云）。换机器按需删除。

### 观测栈

```bash
cd traceops/demo-environment/observability
docker compose up -d     # Prometheus 9090 / Loki 3100 / Tempo 3200 / Grafana 3000
```

## 三条已经立住的设计

1. **证据不信任，先验证**：内容寻址 checksum、四类确定性校验器、报告引用完整性闸门。
   报告引用不存在的证据 → 直接抛 `ReportIntegrityError`，报告不落地。
2. **不确定是合法输出**：数据源缺失、无证据、多候选同分，三种情况都主动降级为
   `INCONCLUSIVE` 并说明缺什么，而不是硬给一个根因。
3. **契约跨语言校验**：Java 生产出的真实 Outbox payload 直接被 `contracts/` 下的
   JSON Schema 校验（离线内联 `$ref`），Python 侧用同一批 schema。

## 已知局限（不粉饰）

- 评估指标的 1.00 来自**合成场景**，不是真实准确率；六个场景按检测规则反向构造，
  存在"规则自证"风险
- F-005 声明了传播链 ground truth，但 P0 只采集单服务证据，**链条未自动校验**
- Grafana MCP 的参数名与权限边界按文档编写，**未经实测**（S2 待执行）
- 无 Redis Streams 消费者、无 LangGraph Checkpoint 持久化（P2）

## 下一步（按依赖）

```
S1 Trace→Log 关联  →  S2 Grafana MCP Tempo PoC  →  S3 事件契约端到端
→ LLM 假设生成节点（解锁 E1–E5）  →  多服务/拓扑感知采集（解锁 F-005 链条）
→ Redis 消费者 + Checkpoint（P2）  →  受控修复（P3）
```
