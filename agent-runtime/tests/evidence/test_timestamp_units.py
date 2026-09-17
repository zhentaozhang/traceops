"""时间戳单位必须自适应，不能硬编码纳秒。

## 缺陷

`LogNormalizer` 与 `_observed_at` 都写死 `int(ns) / 1e9`：

```python
ts = datetime.fromtimestamp(int(ns) / 1e9, tz=UTC)
```

这假定输入**永远是纳秒**。而 Loki 的 `/loki/api/v1/push` 端点虽然
**规范要求纳秒字符串**，实际生态里毫秒/秒级时间戳很常见：

- 部分 Grafana Alloy / Promtail 配置输出毫秒
- 自建 shipper、老版本客户端可能给秒
- 手工构造的测试数据更容易搞错单位

后果：一个秒级时间戳 `1793354520` 会被解析成 **1970-01-01**（差 56 年）。
这不是"看起来不对"，而是：

- 证据时间落在几十年前 → 被 Temporal 校验判为"不在调查窗口"
- 或者更糟：**通过校验**（如果窗口设得宽），然后污染整个时间线
- 传播链的 `observed_after_predecessor` 全部失真

## 正确做法（借鉴 TriageRCA `_normalize_logs`）

按**数量级**判断单位，而不是假定：

```python
while ts.max() > 1e11: ts /= 1000
```

阈值 1e11 的含义：以**秒**为单位的现代时间戳约 1.7e9（2023 年），
而以**毫秒**为单位约 1.7e12。1e11 正好把两者分开。

对应到单值：
- `< 1e11`  → 已是秒
- `< 1e14`  → 毫秒，除以 1e3
- 否则      → 纳秒（或微秒），继续判断

## 为什么不能"干脆只接受纳秒然后报错"

因为来源不可控。宁可把单位判断对，也不要在真实数据到来时才发现解析错误 ——
静默解错一个单位，比明确拒绝更难排查。

## 诚实标注：这是防御性修复，不是修现存 bug

实测确认标准 Loki 路径**不会**产生非纳秒时间戳：

- `/loki/api/v1/push` 拒绝毫秒时间戳（HTTP 500）
- `query_range` 返回 19 位纳秒

所以本模块保护的是未来/非标准来源（自建 shipper、某些 Alloy/Promtail 配置、
手工测试数据、将来接入的其他日志后端）。测试仍然有价值——它钉住
"单位判断必须自适应"这条契约，防止有人把 `coerce_epoch` 换回硬编码 `/1e9`。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traceops.evidence.normalizer import LogNormalizer
from traceops.orchestration.graph import _observed_at
from traceops.domain.models import EvidenceType


EXPECTED = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
SECONDS = int(EXPECTED.timestamp())              # 1793354520
MILLIS = SECONDS * 1000                          # 1793354520000
MICROS = MILLIS * 1000
NANOS = MICROS * 1000


class TestEpochCoercion:
    def test_seconds(self):
        from traceops.evidence.normalizer import coerce_epoch
        assert coerce_epoch(SECONDS) == EXPECTED

    def test_millis(self):
        from traceops.evidence.normalizer import coerce_epoch
        assert coerce_epoch(MILLIS) == EXPECTED

    def test_micros(self):
        from traceops.evidence.normalizer import coerce_epoch
        assert coerce_epoch(MICROS) == EXPECTED

    def test_nanos(self):
        from traceops.evidence.normalizer import coerce_epoch
        assert coerce_epoch(NANOS) == EXPECTED

    def test_accepts_strings(self):
        """Loki 的时间戳是**字符串**形式的整数。"""
        from traceops.evidence.normalizer import coerce_epoch
        assert coerce_epoch(str(NANOS)) == EXPECTED
        assert coerce_epoch(str(SECONDS)) == EXPECTED

    def test_rejects_nonsense(self):
        from traceops.evidence.normalizer import coerce_epoch
        with pytest.raises((TypeError, ValueError)):
            coerce_epoch("not-a-number")


class TestLogNormalizerWithEachUnit:
    @pytest.mark.parametrize("unit", [SECONDS, MILLIS, MICROS, NANOS])
    def test_log_timestamps_parse_to_the_same_instant(self, unit):
        normalizer = LogNormalizer()
        raw = {"streams": [{"stream": {"service": "redis"},
                            "values": [[str(unit), "ERROR redis timeout"]]}]}
        result = normalizer.normalize(raw, max_records=10)
        record_ts = datetime.fromisoformat(result["records"][0]["timestamp"])
        assert abs((record_ts - EXPECTED).total_seconds()) < 1, (
            f"unit {unit} misparsed as {record_ts}")

    def test_seconds_do_not_become_1970(self):
        """硬编码 /1e9 时，秒级时间戳会被解析成 1970 —— 这条就是那个回归。"""
        normalizer = LogNormalizer()
        raw = {"streams": [{"stream": {"service": "redis"},
                            "values": [[str(SECONDS), "ERROR redis timeout"]]}]}
        result = normalizer.normalize(raw, max_records=10)
        record_ts = datetime.fromisoformat(result["records"][0]["timestamp"])
        assert record_ts.year >= 2020, f"parsed as {record_ts} (1970 bug)"


class TestObservedAtWithEachUnit:
    @pytest.mark.parametrize("unit", [SECONDS, MILLIS, MICROS, NANOS])
    def test_observed_at_uses_correct_instant(self, unit):
        raw = {"streams": [{"stream": {"service": "redis"}, "values": [[str(unit), "x"]]}]}
        observed = _observed_at(raw, EvidenceType.LOG, datetime.now(UTC))
        assert abs((observed - EXPECTED).total_seconds()) < 1, (
            f"unit {unit} misparsed as {observed}")
