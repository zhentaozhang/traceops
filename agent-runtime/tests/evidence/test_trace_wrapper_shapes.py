"""TraceNormalizer 必须同时接受两种包装键。

## 缺陷（自观测暴露）

`TraceNormalizer` 只接受：

```python
if "batches" not in raw:
    raise ValueError("trace payload: expected Tempo shape {batches: [...]}")
```

但 **OTLP 规范**（以及 Tempo 的 `/v1/traces` push 端点）用的外层键是
`resourceSpans`：

```
{"resourceSpans": [{"resource": {...}, "scopeSpans": [{"spans": [...]}]}]}
```

`batches` 是项目内部的约定（Tempo `/api/traces/{id}` 返回它）。

后果：任何**符合 OTLP 规范**的 trace 载荷都会被判为"形状错误"而丢弃。
自观测第一次跑就把这点撞出来了 —— TraceOps 自己产出的标准 OTLP
过不了自己的 Normalizer。这个缺口同样会影响它解析外部系统送来的数据。

## 正确行为

两种包装都接受，内部统一成 `batches` 处理：
- `{"batches": [...]}`      —— 内部约定（Tempo 查询 API）
- `{"resourceSpans": [...]}` —— OTLP 规范（push 端点 / 标准 SDK 产出）
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traceops.domain.models import EvidenceType
from traceops.evidence.normalizer import normalize
from traceops.orchestration.graph import _unwrap_raw

OTLP_RESOURCE_SPAN = {
    "resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "redis"}}]},
    "scopeSpans": [{"spans": [{
        "traceId": "a" * 32,
        "spanId": "b" * 16,
        "name": "redis.command",
        "startTimeUnixNano": "1793417000000000000",
        "endTimeUnixNano": "1793417002100000000",
        "status": {"code": "STATUS_CODE_ERROR"},
    }]}],
}


def _normalize(raw):
    now = datetime.now(UTC)
    return normalize(
        evidence_type=EvidenceType.TRACE, source_id="s", incident_id="i", run_id="r",
        service_id="redis", query_template_id="t", query_params={},
        raw_payload=raw, observed_at=now, window=(now, now),
    )


class TestBatchesWrapper:
    def test_internal_batches_shape_still_works(self):
        evidence = _normalize({"batches": [OTLP_RESOURCE_SPAN]})
        assert evidence.normalized_data["total_records"] == 1


class TestResourceSpansWrapper:
    def test_standard_otlp_shape_is_accepted(self):
        """OTLP 规范的外层键是 resourceSpans —— 必须接受。"""
        evidence = _normalize({"resourceSpans": [OTLP_RESOURCE_SPAN]})
        assert evidence.normalized_data["total_records"] == 1
        span = evidence.normalized_data["spans"][0]
        assert span["operation_name"] == "redis.command"
        assert span["service"] == "redis"

    def test_otlp_shape_without_extra_wrapper(self):
        """有些 SDK 直接给一个 resourceSpan 而不是数组。"""
        evidence = _normalize(OTLP_RESOURCE_SPAN)
        assert evidence.normalized_data["total_records"] == 1

    def test_empty_resource_spans_is_empty_evidence(self):
        """空数组是"没有 span"，不是形状错误 —— 该抛出明确的信息。"""
        with pytest.raises(ValueError, match="no spans"):
            _normalize({"resourceSpans": []})
