# TraceOps Console

控制台前端。事故分诊、调查过程、证据溯源、服务拓扑、评测实验室。

当前处于 **A 阶段（免疫区）**：所有页面、组件、图表层和数据形状都已落地，
但数据来自仓库里的真实产物（fixture），还没有接上控制面的 HTTP API。
换句话说——后端怎么改，这个前端都不会散架；后端一到位，只需要换数据源。

## 跑起来

```bash
cd frontend
pnpm install
pnpm dev            # http://localhost:5183
```

> **registry**：本机直连 `registry.npmjs.org` 会超时。装依赖前先设镜像：
>
> ```bash
> NPM_CONFIG_REGISTRY=https://registry.npmmirror.com/ pnpm install
> ```

其它命令：

```bash
pnpm build          # tsc --noEmit + vite build
pnpm typecheck
pnpm lint           # Biome（lint + format 检查）
pnpm lint:fix
pnpm fixtures       # 从仓库真实产物重新生成 fixture
pnpm fixtures:check # fixture 是否已过期（CI 用）
```

## 目录

```
frontend/
├── apps/console/            # 控制台本体（唯一的前端应用）
│   └── src/
│       ├── domain/          # 领域模型：VM 类型、适配器、格式化、证据载荷解析
│       ├── data/            # 数据来源：DataSource 接口 + fixture 实现 + 人工标注
│       ├── ui/              # UI 原语与图表层（不碰业务语义）
│       ├── app/             # 应用壳：导航、命令面板、路由、图标
│       ├── pages/           # 8 个页面
│       ├── styles/          # 设计令牌 + 样式（5 个文件）
│       └── fixtures/        # 页面用的数据
├── scripts/build-fixtures.mjs
└── biome.json
```

## 设计约束

这三条是硬的，写代码时不要绕过去：

1. **组件不 import wire。** 页面和组件只认 `domain/types.ts` 里的 VM 类型，
   永远不直接吃后端 JSON。转换发生在 `domain/adapters.ts`。
2. **解析失败不抛异常。** 缺字段就是 `null`，未知枚举进「其他」并把原值留在
   `raw*` 字段里。后端加了一个枚举值，前端不会白屏。
3. **组件里不写裸色值。** 颜色一律走 CSS 令牌（`tokens.css`）。
   暗色/亮色、紧凑/宽松密度都是换令牌，不是换分支。

## 数据从哪来

`src/data/source.ts` 里的 `DataSource` 接口就是未来的 API 边界：

```ts
interface DataSource {
  listIncidents(): Promise<IncidentVM[]>;
  getIncident(id: string): Promise<IncidentVM | null>;
  listServices(): Promise<string[]>;
  getRun(runId: string): Promise<RunVM | null>;
  getReport(runId: string): Promise<ReportVM | null>;
  getEvidence(runId: string): Promise<EvidenceRefVM[]>;
  getEvidenceDetail(id: string): Promise<EvidenceDetailVM | null>;
  getRelations(runId: string): Promise<EvidenceRelationVM[]>;
  getHealth(): Promise<HealthVM | null>;
  getEvaluation(): Promise<EvaluationVM | null>;
  getTopology(): Promise<TopologyVM | null>;
}
```

现在只有 `FixtureDataSource` 实现它。接后端时新增一个 `ApiDataSource`，
在 `app/App.tsx` 里换一行即可——页面代码一行都不用动。

fixture 分两类：

- **派生的**（`scripts/build-fixtures.mjs` 生成）：报告、评测、拓扑。
  源头是 `诊断示例数据`、`evaluation/reports/*.json`。
  `pnpm fixtures:check` 会在源文件变了而 fixture 没跟上时失败。
- **手写的**（直接提交）：事故列表、调查进度、组件健康。
  它们覆盖了 6 种 stage 状态 × 4 种 waiting-on 的组合，是页面状态的完备集。

## 页面

| 路径                       | 做什么                                                             |
| -------------------------- | ------------------------------------------------------------------ |
| `/`                        | 分诊看板：按 lane（待人工/已失败/进行中/待重试/已完成）分组 + 漏斗 |
| `/incidents`               | 事故列表：筛选、排序、定位到 lane                                  |
| `/incidents/$id`           | 事故工作台：结论、候选与证据、调查时间线、不确定的地方             |
| `/incidents/$id/runs/$run` | 单次运行的报告视图                                                 |
| `/evidence`                | 证据浏览器：指标/日志/追踪/事件/关系五个 tab                       |
| `/topology`                | 服务拓扑：依赖图 + 每个服务上的事故                                |
| `/evaluation`              | 评测实验室：指标、朴素基线对比、消融实验                           |
| `/ops`                     | 运维面板：组件健康、调查漏斗、尚未接入的接口清单                   |

## 约定

- 时间一律走 `domain/format.ts`，显示时区 Asia/Shanghai，原始值保留 UTC。
- 数字列用 `tabular-nums`，避免刷新时抖动。
- 结论有四种：`SUPPORTED` / `REFUTED` / `INCONCLUSIVE` / `ABSTAINED`。
  弃权是一等公民，不是失败。
- `missing_data`（没法查）和 `no_data`（查了没有）在界面上必须分开显示：
  前者不构成反驳，后者可以。
- 「最早异常」是**观测到**的时间，不是告警声称的时间。
