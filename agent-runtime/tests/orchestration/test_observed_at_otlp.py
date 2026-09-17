"""`_observed_at` 必须能读 OTLP trace 的时间戳，而不只是 Jaeger 的。

## 缺陷（与上一轮 OTLP 缺口同源）

```python
elif evidence_type is EvidenceType.TRACE:
    for batch in raw.get("batches") or []:
        for span in batch.get("spans") or []:
            start_us = span.get("startTime")        # ← Jaeger 字段
            if start_us is not None:
                return datetime.fromtimestamp(int(start_us) / 1e6, tz=UTC)
```

只认 Jaeger 的 `batches[].spans[].startTime`（**微秒**）。
而真实 Tempo / OTLP 给的是：

```
batches[].scopeSpans[].spans[].startTimeUnixNano    ← 纳秒，且嵌套在 scopeSpans 里
```

后果：OTLP trace 的 `observed_at` **永远取 fallback（窗口末端）**，于是：

- 时间线失真：所有 trace 证据都被标成"故障窗口末尾观测到"
- `observed_after_predecessor` 判断失效（每条都相等）
- 传播链的时序标记失去意义（而那是 R-008 之外少数能反映因果方向的信号）

上一轮修 `TraceNormalizer` 接受了 OTLP 形状，但取值逻辑没跟上 ——
这是同一个 OTLP 缺口在另一处代码里的另一半。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traceops.domain.models import EvidenceType
from traceops.orchestration.graph import _observed_at

# 2026-10-30T10:02:00Z 的纳秒表示
NANO = 1793354520_000000000
JAGER_US = NANO // 1000


class TestOtlpTraceTimestamps:
    def test_reads_startTimeUnixNano_from_scope_spans(self):
        raw = {"batches": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "redis"}}]},
            "scopeSpans": [{"spans": [{
                "name": "redis.command",
                "startTimeUnixNano": str(NANO),
                "endTimeUnixNano": str(NANO + 2100000000),
            }]}],
        }]}
        observed = _observed_at(raw, EvidenceType.TRACE, datetime.now(UTC))
        assert abs((observed - datetime.fromtimestamp(NANO / 1e9, tz=UTC)).total_seconds()) < 60, (
            f"OTLP startTimeUnixNano must be used, got {observed}")

    def test_reads_resource_spans_wrapper(self):
        """OTLP 规范的外层键是 resourceSpans —— 与 Normalizer 的修复保持一致。"""
        raw = {"resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "redis"}}]},
            "scopeSpans": [{"spans": [{
                "name": "redis.command", "startTimeUnixNano": str(NANO)}]}],
        }]}
        observed = _observed_at(raw, EvidenceType.TRACE, datetime.now(UTC))
        assert abs((observed - datetime.fromtimestamp(NANO / 1e9, tz=UTC)).total_seconds()) < 60

    def test_jaeger_shape_still_works(self):
        """向后兼容：Jaeger 的微秒字段不能被破坏。"""
        raw = {"batches": [{"spans": [{"startTime": JAGER_US}]}]}
        observed = _observed_at(raw, EvidenceType.TRACE, datetime.now(UTC))
        assert abs((observed - datetime.fromtimestamp(JAGER_US / 1e6, tz=UTC)).total_seconds()) < 60

    def test_no_timestamp_falls_back(self):
        raw = {"batches": [{"resource": {}, "spans": [{"operationName": "x"}]}]}
        fallback = datetime(2026, 10, 30, 10, 5, 0, tzinfo=UTC)
        assert _observed_at(raw, EvidenceType.TRACE, fallback) == fallback
