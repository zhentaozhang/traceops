# TraceOps

Evidence-driven Agentic RCA Platform —— 面向微服务系统的智能故障调查与根因分析平台。

> 一次故障从告警到诊断：Agent 分阶段调查 → 证据链可追溯 → 根因假设经过独立验证 →
> 修复建议经权限审批后受控执行。

## 当前状态（P0）

| 平面 | 状态 | 测试 |
|---|---|---|
| Control Plane（Java / Spring Boot） | ✅ 告警接入、事故关联、状态机、Outbox、结果消费、**诊断结论 REST 查询**、**失败自动重试**、**契约运行时校验**、**负责人 / 认领 / 结论确认**、**出站通知** | **237 passing** |
| Investigation Plane（Python / LangGraph） | ✅ 8 节点图（含判别回路）、双层采集、传播链、重试分类、**证据读回**、**跨事故记忆**、**查询缓存** | **1291 passing** |
| Evidence Plane（Python） | ✅ 4 类归一化、内容寻址校验、**SQLite 持久化**、查询模板、证据图 | 同上 |
| Evaluation Plane（Python） | ✅ 12 场景（含三服务级联 F-005、F-011 自然语言线索）+ 18 指标 + runner | **115 passing** |
| Execution Plane（受控修复） | ✅ Action Registry + Policy + Approval + **执行后重新观测** + **不达标自动回滚** + **CLI 入口** | 93 passing |
| Topology（服务依赖） | ✅ 双向语义、环检测、平局裁决 | 19 passing |
| 观测栈（docker compose） | ✅ **已拉起**：Prometheus/Loki/Tempo/Grafana + mcp-grafana | 4 服务 healthy |
| MCP 数据源接入 | ⚠️ Prometheus/Loki 可用；Tempo `get_tempo_trace` 受 Tempo 版本限制 | 13 条形状/签名测试 |

**P0 没有 LLM 参与**：这一版全部是确定性代码，目的是把"证据 → 假设 → 排序 → 报告"
这条数据流先做实，再做模型。E1–E5 消融实验因此还跑不了，见 `评测口径` §4。
想确认"真模型那条路"到底通不通，跑 `python -m evaluation.model_check` ——
没有配置端点时它会明确返回 `UNVERIFIED`，而不是假装成功。

## 文档

| 文档 | 内容 | 状态 |
|---|---|---|












## 仓库结构

```
traceops/

│   └── reference/             # 外部项目（dsh-superagent）逐行取证报告，只读
├── contracts/                 # Java↔Python 版本化事件契约（JSON Schema 单一事实来源）
│   ├── common/                #   事件信封、时间窗口
│   ├── events/v1/             #   investigation.requested/updated/completed/failed
│   └── tests/                 #   Python 侧契约测试（13 passing）
├── control-plane/             # Java 21 · Spring Boot 3.5（状态机 + Outbox + Web + 契约校验）
├── agent-runtime/             # Python 3.11 · LangGraph 1.2（证据引擎 + 规则 RCA + 适配器 + 预算）
├── evaluation/                # 场景 JSON + harness + 指标 + runner CLI
└── demo-environment/
    └── observability/         # Prometheus + Loki + Tempo + Grafana（docker compose）
```

## 快速开始

### 30 秒：看到一次诊断（不需要 Redis / Docker / 模型）

```bash
cd traceops
PYTHONPATH=agent-runtime .venv/bin/python -m traceops.cli.demo
```

用合成遥测跑完整编排，打印"结论 / 为什么信 / 下一步 / 不确定的地方"四段，
并把 JSON 报告写到 `--json <path>`。**不想跑也能看**：
[诊断示例] 是一次真实输出的存档。
数据是合成的，编排与报告是真的 —— 边界写在那个文件的第一段。

### 接入前提：你的遥测必须长成这样

查询模板里的名字是**可声明的**：下面这些是默认值（= 仓库自带演示栈），
换一套采集配置不需要改代码，写一个 JSON 文件即可（见下面的例子）：

| 数据源 | 假设 | 用在哪 |
|---|---|---|
| Prometheus | 指标名 `http_requests_total`，标签 `service`、`status` | 错误率、聚焦窗口判别 |
| Prometheus | 任一指标 `metric{service="X"}` 可查（`qt-metric-series-v1`） | 判别回路 |
| Loki | 标签 `service`、`level`，错误日志 `level="error"` | 日志证据 |
| Tempo | `service.name` 标签，`duration > Nms` 过滤 | 慢追踪证据 |
| 通用 | 服务名与拓扑里的名字**逐字符一致** | 归因、爆炸半径 |

把自己的约定写进一个文件，然后交给 worker：

```json
{
  "requests_total": "http_server_requests_count",
  "service_label": "app",
  "status_label": "status",
  "error_status_regex": "5[0-9][0-9]",
  "log_service_label": "app",
  "log_level_label": "severity",
  "log_error_value": "ERROR",
  "trace_service_label": "service.name"
}
```

```bash
python -m traceops.cli.worker --telemetry-conventions ./conventions.json ...
# 或 TRACEOPS_TELEMETRY_CONVENTIONS=./conventions.json
```

拼错字段名会被拒绝并列出可用字段（`unknown convention field(s): ...`）——
静默沿用默认值会让"约定不匹配"伪装成"系统很健康"。

`scripts/verify_stack_config` 只能校验仓库自带的演示栈配置（`demo-environment/observability`）；
**你自己的集群要自己核这几条** —— 核完再谈诊断质量，否则查回来的每一份数据都会被判成
"空结果"，而报告只能说"查了但没有"。

```bash
PYTHONPATH=agent-runtime .venv/bin/python -m scripts.verify_stack_config   # 仅演示栈
```

### 一条命令验证闭环

```bash
cd traceops
PYTHONPATH=agent-runtime .venv/bin/python -m scripts.smoke_loop     # 闭环冒烟
PYTHONPATH=agent-runtime .venv/bin/python -m traceops.cli.worker --mode memory --once --self-check
```

前者验证 Java→Python→Java 的整个链路装配（含契约校验），后者是可运行的 worker 自检。

### Python 侧（证据引擎 + 调查 + 评估）

```bash
cd traceops
python -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e agent-runtime --no-build-isolation

# 全部测试（agent-runtime 1291 + evaluation 139 + contracts 13 = 1443）
PYTHONPATH=agent-runtime .venv/bin/python -m pytest agent-runtime/tests evaluation contracts -q

# 跑评估：12 场景 + 指标 + 每个场景一份可读诊断（reports/reports/<id>.md）
.venv/bin/python -m evaluation.runner

# 确认"真模型那条路"通不通（没有端点时明确返回 UNVERIFIED，退出码 2）
.venv/bin/python -m evaluation.model_check

# 导出盲测任务包：别人也能用同一套指标测我们（答案在 answers.json，不要给出）
PYTHONPATH=agent-runtime:. .venv/bin/python -m evaluation.blind export --out /tmp/blind-pack
PYTHONPATH=agent-runtime:. .venv/bin/python -m evaluation.blind score --pack /tmp/blind-pack --predictions preds.json
# 自检：用我们自己的报告当预测，和内部 AC@1 对账
PYTHONPATH=agent-runtime:. .venv/bin/python -m evaluation.blind selfscore --pack /tmp/blind-pack
```

### Java 侧（Control Plane）

```bash
cd traceops/control-plane
mvn -s .mvn/settings.xml -B test        # 237 tests
mvn -s .mvn/settings.xml spring-boot:run # :8081

curl -X POST localhost:8081/api/v1/alerts -H 'Content-Type: application/json' -d '{
  "alerts":[{"fingerprint":"fp-demo","status":"firing","service":"order-service",
             "severity":"critical","summary":"redis pool exhausted"}]}'
```

### 运维可见性

同一套数据现在有三个"不用翻日志"的入口：

| 端点 | 回答什么 |
|---|---|
| `GET /api/v1/incidents/{id}` | 这条事故**此刻卡在谁那里**（`stage.kind` / `stage.waitingOn` / `stage.retryable`），而不只是"流程走到哪"。`INVESTIGATING` 一个词背后有四种处境：等认领、在跑、卡退避、已经死了 |
| `GET /api/v1/status` | 三条等待读数：outbox 积压（**最老一条等了多久**，≥60s 降级）、重试（等退避 N 条 / 已过期但没发出去的 N 条）、进度存储淘汰与丢弃计数 |
| `GET /api/v1/diagnostics/funnel?since=` | 告警 → 事故 → 调查 → 完成/失败 → 需人看，**每格由自己的来源数出来**，不靠上一步减下一步 |

### 受控修复（操作员带外执行）

修复不由控制面代劳，也不由模型直接动手 —— 三步都留在终端里，每一步都过一道闸：

```bash
# 1. 提案：构造计划、过策略，落一条审批请求（打印 request_id 与 plan_hash）
PYTHONPATH=agent-runtime .venv/bin/python -m traceops.cli.remediate --approval-db state/approvals.db \
  plan --incident inc-1 --action scale_replicas --target order-service \
       --parameters '{"replicas": 4}' \
       --rollback '{"action": "scale_replicas", "parameters": {"replicas": 2}}' \
       --reason "shed load during the incident" --error-rate-below 0.05 --as alice

# 2. 审批：必须有 APPROVER 角色，且不能是自己提的（职责分离）
... approve --request apr-... --as bob --reason "approved for this window"

# 3. 执行：参数与审批时的计划逐字节比对（哈希不一致直接拒绝），执行后复验，不达标自动回滚
... execute --request apr-... --incident inc-1 --action scale_replicas --target order-service \
            --parameters '{"replicas": 4}' --reason "..." --error-rate-below 0.05 \
            --as carol --executor-config deploy/executor.json
```

没有 `--executor-config` 时**拒绝真实写操作**（干跑仍可用）：动作 → argv 的映射由
部署方声明，模型输出的字符串永远进不了命令。同样的，`--approval-db` 不给就是内存库，
重启即失效 —— 这一点在 `运行手册` 的部署清单里。

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

## 本轮最重要的修正：依赖归因

```
order-service 的日志： "ERROR redis timeout - connection pool exhausted"
```

按"谁上报"归因 → 得出 order-service 拥有 redis 池（**错**）
按"日志在说谁"归因 → redis 才是根因服务（**对**）

`detectors.attribute_evidence` 是唯一事实来源，假设生成 / 证据关联 / 评估 precision 全部走它。
修正后 F-001~F-005 的 rank 1 全部指向真正的下游依赖服务。

## 本轮补上的关键能力

| 能力 | 为什么必须有 |
|---|---|
| **Redis RESP2 传输层** | 手写流式解析（逐字节喂入测试）；暴露了 `redis_source` 的配额 bug |
| **Run 账本持久化** | 内存去重重启即失效 → 同一 run 被重复调查 |
| **证据保留策略** | 三条硬约束：未关闭不删、被引用不删、默认 dry-run |
| **双向契约验证** | Java 用手写严格解析器，schema 合法不代表它能吃下 |


| 能力 | 为什么必须有 |
|---|---|
| **证据 SQLite 持久化** | 进程崩溃后内存证据全丢 = 可信性归零；篡改能被 checksum 发现 |
| **重试分类** | 预算耗尽重试只是烧钱；用户取消不该被"好心"重试；未知原因默认不重试 |
| **传播链 vs 候选根因拆分** | 旧设计逼系统给症状服务编造故障机制 |
| **双层采集** | 调查只向下游（防误判根因）与传播链需要上游证据的冲突 |
| **查询模板** | Agent 不能自己生成 PromQL/LogQL（防注入、可审计） |
| **checkpoint 恢复** | `RunContext` 不可序列化曾让 checkpoint 完全不可用 |
| **诊断结论 REST 查询** | 结论只活在事件里，一次 ACK 后就只剩账本一行 |
| **死信流** | 坏消息被静默 ACK 会让数据无声消失 |

## 已知局限（不粉饰）

- 评估指标全部来自**合成场景**，不是真实准确率；12 个场景按检测规则反向构造，
  存在"规则自证"风险。当前 `AC@1=0.91`（不是满分：F-000 应弃权、F-010 是设计内的平局）
- **`AC@3`/`AC@5` 不可引用**：候选池最大只有 4，`AC@5` 是数学恒等式。
  只看 `lift@1=3.64`（对阵 `chance@1=0.25`）
- F-005 的 `chain=1.00` 已通过（拆出 `propagation_chain` + 双层采集后修复）。
  F-005 的 citation precision 实测 **0.60**（此前文档写的 0.10 是过期值：
  根因是级联场景的分母算错，已修，见 EVALUATION.md）。剩余差距是"多查服务"
  与"引用要精确"的固有权衡
- **E4/E5 消融目前测不出差异**，且这是**结构性**的：语义闸门的触发条件与
  候选生成条件互斥（`coverage==0` 不可达），矛盾检测也无法让 `REFUTED` 成立。
  确定性路径上这两个闸门是防御性代码，只在模型提出低质量假设时才有效
  （`agent-runtime/tests/rca/test_gate_reachability.py` 钉住这个边界）
- `must_identify_rate=0.91`（不是 1.00）：F-007 的双故障只报出了 redis，
  `billing-api` 从未被采集。这是真实的漏报，不是口径问题
- **Tempo 2.5 下 `get_tempo_trace` 恒 404**（Tempo 缺 `/api/v2/traces` 端点）。
  已降级为用 `search_tempo_traces` 的 spanSet 构造证据，并在
  `Evidence.normalized_data` 标记 `degraded=True`。升到 Tempo ≥ 2.6 可恢复完整 span 树
- MCP 的 Prometheus/Loki 参数名与**响应形状**此前全错（形状不对时证据会被静默丢弃）。
  已修正，并有 13 条用真实响应做 fixture 的测试锁定
- LangGraph Checkpoint 只用于单次运行：worker 调用 `run_investigation` 时
  **未传 `checkpointer` / `thread_id`**，跨进程崩溃恢复尚未接线（P2）
- 无 CI：`.github/workflows` 不存在，S3 的「CI 契约测试绿」尚未满足

## 下一步（按依赖）

```
S3 事件契约 CI 回填  →  LLM 假设生成节点（解锁 E1–E5）
→ 真实靶场注入 S4/S5（解锁真实准确率）  →  Tempo 升版恢复完整 span 树
→ Redis 消费者 Checkpoint 接线（P2）  →  受控修复（P3）
```
