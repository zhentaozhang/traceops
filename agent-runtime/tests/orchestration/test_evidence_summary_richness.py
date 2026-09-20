"""给模型的证据摘要太贫瘠，通电后模型无法提假设。

## 缺陷（参考仓库深挖时指出，实测确认）

```python
def _evidence_summary(evidence_list):
    summary = {}
    for evidence in evidence_list:
        entry = summary.setdefault(key, {"count": 0, "services": []})
        entry["count"] += 1
```

只给 `{类型: {count, services}}`。设计初衷是**省 token + 防注入**（日志原文不进 Prompt），
这个初衷是对的。但它走得太远：

模型拿到的是

```json
{"logs": {"count": 3, "services": ["redis"]}, "metrics": {...}}
```

它知道"有 3 条日志，关于 redis"，但**不知道日志说了什么**。
于是它只能靠 `service_hint` 猜，提不出有信息量的假设 ——
那正是"模型假设质量不如规则"这类失败的真实来源之一。

## 正确的折中（不是"把原文塞进去"）

关键区分：**归一化后的结构化特征**是安全的，**原始日志文本**是不安全的。

| 可以给 | 不可以给 |
|---|---|
| 证据类型、归属服务、观测时间窗口 | 日志原文（提示注入面） |
| **异常数值摘要**（错误率峰值、是否超阈值） | 完整 payload |
| **机制关键词命中**（如 "pool"/"timeout" 命中与否） | 大段自由文本 |
| **慢 span 的操作名与耗时**（结构化字段） | …… |

`span.operation_name` 是结构化字段（来自 OTLP），不是自由文本。
错误率峰值是数字。这些给模型是安全的，而且信息量远高于 count。

## 为什么这不能等到 P1

摘要贫瘠时，模型路径失败会被误读成"模型不行"，而不是"我们没给它足够信息"。
那会让 E1/E2 的消融结论从一开始就失真 —— 而消融正是 P1 的核心验收条件。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.domain.models import Evidence, EvidenceType
from traceops.orchestration.graph import _evidence_summary

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)


def _metric(value: str = "0.42") -> Evidence:
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.METRIC,
        source_id="prom", service_id="redis", observed_at=TS,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id="qt-error-rate-v1", query_params={},
        normalized_data={"kind": "metric", "result_type": "matrix", "result_count": 1,
                         "total_records": 1,
                         "series": [{"metric": {}, "values": [[TS.timestamp(), value]]}]},
        checksum="")


def _log(message: str = "ERROR redis timeout pool exhausted") -> Evidence:
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.LOG,
        source_id="loki", service_id="redis", observed_at=TS,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id="qt-error-logs-v1", query_params={},
        normalized_data={"kind": "log", "total_records": 1,
                         "records": [{"timestamp": TS.isoformat(), "message": message,
                                      "labels": {"level": "error"}}]},
        checksum="")


def _slow_trace() -> Evidence:
    return Evidence(
        incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.TRACE,
        source_id="tempo", service_id="redis", observed_at=TS,
        window_start=TS - timedelta(minutes=2), window_end=TS + timedelta(minutes=3),
        query_template_id="qt-slow-traces-v1", query_params={},
        normalized_data={"kind": "trace", "total_records": 1, "spans": [
            {"trace_id": "t", "span_id": "s", "service": "redis",
             "operation_name": "redis.command", "duration_us": 2_100_000, "error": True}]},
        checksum="")


class TestSummaryCarriesUsableSignal:
    def test_metric_summary_includes_peak_value(self):
        """模型应当知道"错误率峰值是多少"，而不只是"有一个指标"。"""
        summary = _evidence_summary([_metric("0.42")])
        metrics = summary["metrics"]
        assert any("peak" in k.lower() or "0.42" in str(v)
                   for k, v in (metrics.items() if isinstance(metrics, dict) else [])), (
            f"metric summary has no numeric signal: {metrics}")

    def test_metric_summary_flags_whether_it_exceeded_threshold(self):
        summary = _evidence_summary([_metric("0.42")])
        assert "above_threshold" in str(summary) or "exceeds" in str(summary).lower(), (
            f"threshold verdict missing: {summary}")

    def test_log_summary_reports_keyword_hits_not_raw_text(self):
        """给机制关键词命中，**不给日志原文** —— 防注入的初衷不能丢。"""
        summary = _evidence_summary([_log()])
        text = str(summary)
        assert "pool" in text.lower() or "timeout" in text.lower(), (
            f"mechanism keywords missing: {text}")
        # 原文绝不能整句出现（提示注入面）
        assert "ERROR redis timeout pool exhausted" not in text, (
            "raw log text must not enter the prompt")

    def test_trace_summary_includes_operation_and_duration(self):
        """span 的操作名与耗时是结构化字段，给模型是安全的、且信息量大。"""
        summary = _evidence_summary([_slow_trace()])
        text = str(summary)
        assert "redis.command" in text, f"operation name missing: {text}"
        assert "2100" in text or "2.1" in text, f"duration missing: {text}"

    def test_counts_are_still_present(self):
        """摘要变丰富，但不能丢掉原有的计数 —— 它是"有多少证据"的锚点。"""
        summary = _evidence_summary([_metric(), _log(), _slow_trace()])
        assert summary["metrics"]["count"] == 1
        assert summary["logs"]["count"] == 1
        assert summary["traces"]["count"] == 1

    def test_summary_stays_small(self):
        """丰富不等于臃肿：摘要整体必须保持很小（上下文成本是硬约束）。"""
        import json
        many = [_log(f"ERROR redis timeout #{i}") for i in range(50)]
        size = len(json.dumps(_evidence_summary(many)))
        assert size < 2000, f"summary too large for a prompt: {size} bytes"
