# TraceOps 观测栈（P0）

最小可用的观测后端：Prometheus（指标）+ Loki（日志）+ Tempo（链路）+ Grafana（统一查询）。

```bash
docker compose up -d
docker compose ps          # 四个服务应为 healthy
curl -s localhost:9090/-/healthy
curl -s localhost:3100/ready
curl -s localhost:3200/ready
```

## 与代码的对应关系

| 组件 | 代码入口 | 默认地址 |
|---|---|---|
| Prometheus | `PrometheusAdapter` | `http://localhost:9090` |
| Loki | `LokiAdapter` | `http://localhost:3100` |
| Tempo | `TempoAdapter` | `http://localhost:3200` |
| Grafana | `GrafanaMCPClient` / `MCPDatasourceAdapters` | `http://localhost:3000/mcp` |

数据源 uid 固定为 `prometheus` / `loki` / `tempo`，与 `MCPDatasourceAdapters` 构造参数默认值一致。

## 刻意留空的两处

1. **`prometheus.yml` 的 `scrape_configs` 为空**：靶场服务未就位前，Prometheus 不应有指标。
   这让"查询查不到数据 → 调查给出 INCONCLUSIVE"的失败路径可以被真实验证，而不是被假数据掩盖。
2. **无故障注入**：F-001~F-005 的注入需要靶场（TicketFlow 或等价服务），见
   `技术验证记录` 的 S4 / S5。

## Grafana MCP

Grafana 11.1 起内置 MCP Server，路径 `/mcp`。生产使用前必须：

- 建只读 Service Account（Viewer 角色），**不要**用 admin
- 只授予 `datasources:read` 与查询类权限，禁止 dashboard:write
- Grafana Cloud 需在组织设置里显式启用 MCP Server

未配置 SA 时，`GrafanaMCPClient` 会因 401 失败——这是预期行为，不要退化成匿名访问。
