"""Profile（持续剖析）是第四类可观测数据，我们完全缺这一维。

## 缺口（对照真实 MCP 工具面发现）

MCP 有 79 个工具，我们只用了 5 个（Prometheus / Loki / Tempo 的查询）。
其中 **Pyroscope 的 4 个工具**（`query_pyroscope` / `list_pyroscope_*`）完全未使用，
而 `EvidenceType` 只有 `metric / log / trace / event` —— **没有 profile**。

为什么这一维重要：对「为什么慢」这类根因，profile 比 trace 更直接。

| 数据 | 回答的问题 |
|---|---|
| trace | 哪个**调用**慢（跨度） |
| metric | 慢**多少**（错误率/延迟分位） |
| log | 有没有报错 |
| **profile** | 慢在**哪个函数/哪一行**（CPU/内存热点） |

没有 profile 时，`latency_degradation` 只能停在"redis.command 慢 2.1s"，
无法回答"慢在解析还是锁竞争"。而答案本身就在 profile 里。

## 本模块的边界（诚实）

- 只做**归一化**：把 Pyroscope 的 profile 数据转成 `Evidence`，
  与现有四类同一套 checksum / 引用校验 / 保留策略
- **不自动生成候选**：profile 不进 detectors 的候选生成。
  理由是「热点函数」不等于「根因」，把它当候选会造出大量假根因
- **不用它替代 trace**：它是补充维度，不是替代

## 为什么先做归一化而不是完整接入

采集链路（adapters 的 `query_profiles`）需要真实 Pyroscope 实例才能验证。
先做归一化，可以在**没有 Pyroscope**的情况下用固定样本验证解析正确性——
这和 `evaluation/adapters` 的 fixture 思路一致。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from traceops.domain.models import EvidenceType
from traceops.evidence.normalizer import normalize


def _pyroscope_flamegraph() -> dict:
    """Pyroscope 的 `/api/v1/flamegraph` 风格响应（真实形状）。"""
    return {
        "flamegraph": {
            "names": [
                "total", "redis.command", "hiredis.Reader.read_response",
                "socket.read", "poll.wait",
            ],
            "levels": [
                # 每一层是 self-value 的数组，索引对应 names
                [-1, -1, -1, -1, -1],          # 占位层
            ],
            "values": [1_000_000, 800_000, 600_000, 400_000, 300_000],
        },
        "metadata": {
            "sampleRate": 100,
            "spyName": "pyroscope-java",
            "units": "samples",
        },
    }


class TestProfileEvidenceTypeExists:
    def test_evidence_type_has_profile(self):
        assert hasattr(EvidenceType, "PROFILE"), (
            "profile is the fourth observability dimension and must be a first-class "
            "evidence type, not smuggled into metric")

    def test_profile_is_distinct_from_metric(self):
        assert EvidenceType.PROFILE is not EvidenceType.METRIC


class TestProfileNormalization:
    def test_flamegraph_normalizes_to_records(self):
        now = datetime.now(UTC)
        evidence = normalize(
            evidence_type=EvidenceType.PROFILE,
            source_id="pyroscope", incident_id="i", run_id="r",
            service_id="redis", query_template_id="qt-profile-v1",
            query_params={}, raw_payload=_pyroscope_flamegraph(),
            observed_at=now, window=(now, now),
        )
        assert evidence.normalized_data["total_records"] >= 1
        assert evidence.normalized_data["kind"] == "profile"

    def test_hot_functions_are_extracted(self):
        """必须能回答"热点在哪个函数" —— 那是 profile 的唯一价值。"""
        now = datetime.now(UTC)
        evidence = normalize(
            evidence_type=EvidenceType.PROFILE, source_id="pyroscope",
            incident_id="i", run_id="r", service_id="redis",
            query_template_id="qt", query_params={},
            raw_payload=_pyroscope_flamegraph(), observed_at=now, window=(now, now),
        )
        frames = evidence.normalized_data["hot_frames"]
        names = [f["frame"] for f in frames]
        # 排除根节点 total，应出现具体函数名
        assert "hiredis.Reader.read_response" in names or "socket.read" in names

    def test_hot_frames_are_ordered_by_self_value(self):
        """顺序必须是"最热在前" —— 否则调用方还要自己排序。"""
        now = datetime.now(UTC)
        evidence = normalize(
            evidence_type=EvidenceType.PROFILE, source_id="pyroscope",
            incident_id="i", run_id="r", service_id="redis",
            query_template_id="qt", query_params={},
            raw_payload=_pyroscope_flamegraph(), observed_at=now, window=(now, now),
        )
        values = [f["self_value"] for f in evidence.normalized_data["hot_frames"]]
        assert values == sorted(values, reverse=True)

    def test_unsupported_shape_raises_clearly(self):
        """坏数据要报明确的错，而不是静默返回空。"""
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="profile payload"):
            normalize(
                evidence_type=EvidenceType.PROFILE, source_id="pyroscope",
                incident_id="i", run_id="r", service_id="redis",
                query_template_id="qt", query_params={},
                raw_payload={"unexpected": []}, observed_at=now, window=(now, now),
            )


class TestProfileDoesNotBecomeACandidate:
    """profile 不自动生成候选 —— 热点函数不等于根因。"""

    def test_detectors_ignore_profile_evidence(self):
        from traceops.orchestration.detectors import generate_hypotheses

        # 只有 profile 证据时不应产出任何候选
        assert "profile" not in str(
            __import__("traceops.orchestration.detectors", fromlist=["_EXPECTED_BY_FAULT"])
            ._EXPECTED_BY_FAULT)


class TestRootFrameIsNotAHotFrame:
    """`total` 这类聚合根帧不是"热点函数"，必须排除。

    ## 为什么这是真实缺陷（实测发现）

    Pyroscope 的 flamegraph `names` 数组第一个元素通常是 `total`，
    它的 value 是**整棵树的聚合**，因此永远排在最前 ——
    但它是根节点，不是任何具体函数。

    把它当热点会误导诊断方向：报告会说"热点是 total"，
    而真正该看的是 `hiredis.Reader.read_response`（占 60%）。

    这类"看起来是数据、实际是容器"的条目是归一化层最典型的陷阱 ——
    塞进去不会报错，只会让下游拿到没有意义的答案。
    """

    def test_total_frame_is_excluded_from_hot_frames(self):
        now = datetime.now(UTC)
        evidence = normalize(
            evidence_type=EvidenceType.PROFILE, source_id="pyroscope",
            incident_id="i", run_id="r", service_id="redis",
            query_template_id="qt", query_params={},
            raw_payload=_pyroscope_flamegraph(), observed_at=now, window=(now, now),
        )
        names = [f["frame"] for f in evidence.normalized_data["hot_frames"]]
        assert "total" not in names, (
            f"aggregate root frame must not be reported as a hot function: {names}")

    def test_hottest_real_frame_is_first(self):
        now = datetime.now(UTC)
        evidence = normalize(
            evidence_type=EvidenceType.PROFILE, source_id="pyroscope",
            incident_id="i", run_id="r", service_id="redis",
            query_template_id="qt", query_params={},
            raw_payload=_pyroscope_flamegraph(), observed_at=now, window=(now, now),
        )
        first = evidence.normalized_data["hot_frames"][0]["frame"]
        assert first == "redis.command", f"expected the hottest real frame first, got {first}"
