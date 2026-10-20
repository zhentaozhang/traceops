"""策略旋钮的声明式定义。

## 为什么需要一份声明

旋钮原先散落在三个地方，各自裸奔：

- `orchestration/detectors.py:26-27` —— `ERROR_RATE_THRESHOLD = 0.05`、
  `SLOW_SPAN_THRESHOLD_SECONDS = 0.5`
- `rca/ranking.py:23-28` —— 六个 `_W_*` 权重
- `runtime/budget.py` —— `MAX_TOOL_CALLS` / `MAX_ROUNDS` / `MAX_TOKENS` / `deadline_seconds`

每个都是模块级常量，没有类型、没有取值范围、没有说明。后果：

1. **改错不会报错**：把 `error_rate` 写成 `5`（本意 5%）不会有人拦，
   阈值从此永远不触发 —— 而调查仍然"成功"、报告仍然 SUPPORTED。
2. **没法比较两次配置**：`strategy_components()` 把值塞进字典，
   但"这次和上次差在哪"只能靠肉眼看两个 dict。
3. **默认值与实现可以漂移**：如果有人给某个常量换了默认值，
   文档里的数字不会跟着变。

super-agent 的做法是一份 `FieldSpec` 声明同时驱动三件事
（`src/sources/kind.ts:16-22`：校验、设置表单、设置差异计算）。
这里抄的是这个"一份声明、三个消费者"的结构：校验、差异、以及
`strategy_components()` 的取值来源。
"""
