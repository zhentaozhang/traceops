# TraceOps Event Contracts

Java Control Plane 与 Python Agent Runtime 之间所有异步事件的**单一事实来源**（Single Source of Truth）。

## 结构

```
contracts/
├── common/
│   ├── event-envelope.schema.json   # 统一事件信封（去重 / 追踪 / 版本）
│   └── time-range.schema.json       # 统一时间窗口
└── events/
    └── v1/
        ├── investigation-requested.schema.json    # Control → Agent：启动调查
        ├── investigation-updated.schema.json      # Agent → Control：进度更新（高频）
        ├── investigation-completed.schema.json    # Agent → Control：终态成功
        └── investigation-failed.schema.json       # Agent → Control：终态失败/取消
```

## 消息格式

Redis Streams 消息体 = 符合事件 schema 的完整 JSON，固定两层：

```json
{
  "envelope": {
    "event_id": "evt-<uuid4>",
    "event_type": "investigation.requested",
    "schema_version": "1.0",
    "occurred_at": "2026-10-30T02:00:00Z",
    "correlation_id": "inc-001",
    "causation_id": null,
    "aggregate_id": "inv-001",
    "aggregate_version": 1,
    "producer": "control-plane"
  },
  "payload": { "...": "由各事件 schema 定义" }
}
```

字段语义速查：

| 字段 | 用途 |
|---|---|
| `event_id` | 幂等去重的唯一键（消费端必须检查） |
| `correlation_id` | 跨系统全链路追踪（通常 = incident_id） |
| `causation_id` | 事件因果链（哪个事件触发了本事件） |
| `aggregate_id` + `aggregate_version` | 检测同一次 Run 的事件乱序 |
| `schema_version` | 消费端只接受支持的主版本 |

## 规则

1. **修改契约 = 修改本目录的 JSON Schema 并 bump 版本**，Java/Python 两端不得各自私改字段
2. **只加不删**：新增字段用 optional 向后兼容；删除/改义字段必须升主版本并新增 v2 目录
3. **CI 三道闸**：
   - Schema 本身符合 JSON Schema 2020-12
   - 样例消息通过两端校验（Java: networknt / Python: jsonschema）
   - 跨语言 round-trip 测试（Java 生产 → Python 消费 → Python 回发 → Java 消费）
4. **大对象不进消息体**：报告、原始日志、Trace 内容走持久化存储，消息只传引用（`rca_report_ref`）
5. 代码生成（Java record / Pydantic model）只是开发便利，**运行时校验不可省略**

## 消费端幂等要求

- `investigation.requested`：按 `payload.investigation_run_id` 去重，重复事件直接 ACK 跳过
- `investigation.updated`：按 `payload.aggregate_version` 丢弃乱序/回退事件
- `investigation.completed` / `investigation.failed`：按 `event_id` 去重；终态事件幂等应用到 Incident 状态机
