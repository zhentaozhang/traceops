"""观测时间不得超前于采集时刻。

## 为什么必须锁住这条

`Evidence` 有一条不变量：`collected_at >= observed_at`
（采集时刻不能早于"数据观测时刻"）。它防的是把未来数据当成已观测到的事实。

`_observed_at` 从数据本身取观测时间，取不到才用窗口结束时间。问题在于
**查询窗口通常包含"现在之后"的一小段**（告警刚触发时窗口末端在未来），
于是最后一个数据点的时间戳可能落在未来，`observed_at` 就跑到采集时刻之后，
`Evidence` 构造期直接 ValidationError。

后果不是"报个错"而是**证据被全部丢弃**：`_collect_node` 把 ValidationError
当"这个数据源查不到数据"记进 missing_data 然后 continue。实测跑 MCP 全链路时，
metric 和 log 两类证据都因此不见了，只有一条时间恰好没超前的 log 侥幸留下。
最终报告仍可能给出 SUPPORTED，用的是残缺证据 —— 这比直接失败更危险。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import EvidenceType
from traceops.orchestration.graph import _observed_at


class TestObservedAtNeverExceedsNow:
    def test_future_metric_timestamp_is_clamped_to_now(self):
        """窗口延到未来 → 指标最后一点落在未来 → 必须夹到"现在"。"""
        window_end = datetime.now(UTC) + timedelta(minutes=10)
        future_ts = (datetime.now(UTC) + timedelta(minutes=5)).timestamp()
        raw = {"resultType": "matrix",
               "result": [{"metric": {}, "values": [[future_ts, "0.42"]]}]}

        observed = _observed_at(raw, EvidenceType.METRIC, window_end)

        assert observed <= datetime.now(UTC) + timedelta(seconds=1), (
            f"observed_at {observed} is in the future; Evidence would reject it")

    def test_future_log_timestamp_is_clamped_to_now(self):
        window_end = datetime.now(UTC) + timedelta(minutes=10)
        future_ns = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp() * 1e9)
        raw = {"streams": [{"stream": {"service": "s"},
                            "values": [[str(future_ns), "boom"]]}]}

        observed = _observed_at(raw, EvidenceType.LOG, window_end)

        assert observed <= datetime.now(UTC) + timedelta(seconds=1), (
            f"observed_at {observed} is in the future")

    def test_future_span_timestamp_is_clamped_to_now(self):
        window_end = datetime.now(UTC) + timedelta(minutes=10)
        future_us = int((datetime.now(UTC) + timedelta(minutes=5)).timestamp() * 1e6)
        raw = {"batches": [{"spans": [{"startTime": future_us}]}]}

        observed = _observed_at(raw, EvidenceType.TRACE, window_end)

        assert observed <= datetime.now(UTC) + timedelta(seconds=1), (
            f"observed_at {observed} is in the future")

    def test_past_timestamp_is_preserved_not_clamped(self):
        """历史数据的观测时间必须原样保留，不能被"夹到现在"破坏时间线。"""
        past = datetime.now(UTC) - timedelta(minutes=30)
        raw = {"resultType": "matrix",
               "result": [{"metric": {}, "values": [[past.timestamp(), "1"]]}]}

        observed = _observed_at(raw, EvidenceType.METRIC, datetime.now(UTC))

        assert abs((observed - past).total_seconds()) < 2, (
            f"past timestamp was altered: {observed} vs {past}")
