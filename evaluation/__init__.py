"""TraceOps 评估平面（蓝皮书 §10 Evaluation Lab）。

与 agent-runtime 分离：
- agent-runtime 负责"一次调查怎么跑"
- evaluation 负责"跑 N 次、注入故障、按 ground truth 打分"

指标口径遵循 REVISIONS-1.1 R8：服务定位 / 故障机制 / 证据质量 / 弃权 分开统计。
"""
