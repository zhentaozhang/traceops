"""PROFILE 有归一化器、有 EvidenceType，却没有任何生产者。

## 被替换的缺陷

`EvidenceType.PROFILE` 与 `ProfileNormalizer` 早就在库里（还带测试：
`total` 根帧必须排除、非法 payload 要抛错），但 `_PLAN_TEMPLATES` 里
**没有 profile 条目**、工具面上也没有 `query_service_profile`。
于是这个证据类型从采集到报告的整条链是断的 —— 声明了却没有生产者。

## 为什么现在只补"取数口"，不补 HTTP 客户端

没有真实的 Pyroscope/Parca 端点可验，写一个"看起来能连"的 HTTP 适配器
只会让下次部署时以为它已经好了。这里只定义两样东西：
  - `DatasourceAdapters.capabilities` 里的 `profile` 能力（按是否真的注入了适配器派生）；
  - `query_service_profile` 取数口，输出形状对齐 `ProfileNormalizer` 的入参。
真正接哪家，由部署方实现 `flamegraph(service=..., start=..., end=...)`。

## 为什么只在判别轮查

火焰图很贵（一个窗口的采样数据动辄几 MB），而它的用途很窄：
"CPU 花在哪个函数上"。首轮就查等于为每一趟调查多付一次钱，
而 90% 的调查在首轮就收敛了。只有当两个候选分不开、且并列的机制里
**有** `latency_degradation` 时，热点函数才可能提供新的分辨力。

## 为什么不进候选生成

`agent-runtime/tests/tools/test_profile_evidence.py` 已经有一条断言：
`"profile" not in _EXPECTED_BY_FAULT`。热点函数说的是"时间花在哪"，
不是"谁先坏"，拿它生成候选会把性能热点误报成故障根因。
所以 profile 只做**摘要补充**：函数名进模型输入，结论仍由既有规则决定。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from traceops.domain.models import EvidenceType
from traceops.evidence.normalizer import normalize
from traceops.evidence.store import EvidenceStore  # noqa: F401  (端到端用例用)
from traceops.orchestration.graph import (
    CTX_CONFIG_KEY,
    InvestigationRequest,
    RunContext,
    _adapter_supports,
    _evidence_summary,
    _profile_items,
    run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.tools.adapters import DatasourceAdapters
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = TS - timedelta(minutes=2)
WINDOW_END = TS + timedelta(minutes=3)


def _request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id="run-profile",
        incident_id="inc-profile",
        environment="demo",
        service_hint="checkout-service",
        fault_start=TS,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )


def _profile_evidence():
    raw = {
        "flamegraph": {
            "names": [
                "total",
                "com.acme.PaymentClient.charge",
                "com.acme.OrderService.handle",
            ],
            "values": [1200, 900, 120],
        }
    }
    return normalize(
        evidence_type=EvidenceType.PROFILE,
        source_id="pyroscope",
        incident_id="inc-profile",
        run_id="run-profile",
        service_id="payment-service",
        query_template_id="qt-profile-v1",
        query_params={"service": "payment-service"},
        raw_payload=raw,
        observed_at=TS,
        window=(WINDOW_START, WINDOW_END),
    )


# ---------- 1. 热点函数进得了模型输入，原始火焰图进不去 ----------

class TestTheHotFrameReachesTheModel:
    def test_the_hottest_frame_is_summarised(self):
        summary = _evidence_summary([_profile_evidence()])

        entry = summary["profiles"]
        # 函数名是结构化字段（不是日志自由文本），可以安全进 Prompt
        assert entry["hot_function"] == "com.acme.PaymentClient.charge"
        assert entry["hot_self_value"] == 900.0

    def test_the_root_frame_never_becomes_the_hot_function(self):
        # `total` 是聚合根帧（整张火焰图的和），把它当热点等于说"热点是全部"
        summary = _evidence_summary([_profile_evidence()])

        assert summary["profiles"]["hot_function"] != "total"

    def test_raw_flamegraph_text_is_not_copied_into_the_summary(self):
        summary = _evidence_summary([_profile_evidence()])

        assert "com.acme.OrderService.handle" not in str(summary)


# ---------- 2. 能力门控：没装 profile 适配器就不进计划 ----------

class TestCapabilityGating:
    def _ctx(self, adapters) -> RunContext:
        return RunContext(
            store=EvidenceStore(),
            adapters=adapters,
            budget=BudgetManager(max_tool_calls=20),
            topology=ServiceTopology.from_edges([("checkout-service", "payment-service")]),
            scope_depth=1,
        )

    def test_an_adapter_without_profile_declares_no_profile_capability(self):
        adapters = DatasourceAdapters()

        assert "profile" not in adapters.capabilities
        assert _adapter_supports(self._ctx(adapters), "profile") is False

    def test_profile_items_are_empty_without_the_capability(self):
        items = _profile_items(_request(), self._ctx(DatasourceAdapters()),
                              ["payment-service"])

        # 计划里每一条都会变成一次工具调用，调用失败又会写进 missing_data ——
        # 报告于是说"缺失数据"，而真相是"根本没配"
        assert items == []

    def test_a_configured_profile_adapter_enables_the_items(self):
        class _Profile:
            def flamegraph(self, *, service, start, end):
                return {"flamegraph": {"names": ["total"], "values": [1]}}

        ctx = self._ctx(DatasourceAdapters(profile=_Profile()))

        items = _profile_items(_request(), ctx, ["payment-service"])

        assert [item["tool"] for item in items] == ["query_service_profile"]
        assert items[0]["evidence_type"] is EvidenceType.PROFILE
        # id 只是一个审计标签（这条查询不走模板库的渲染路径），
        # 但它是 Evidence 的必填字段，所以必须有一个稳定的值
        assert items[0]["query_template_id"] == "qt-profile-v1"


# ---------- 3. 端到端：只在判别轮查，且只有延迟类候选会引来它 ----------

def _trace_payload(epoch: float) -> dict:
    return {
        "traces_considered": 1,
        "batches": [
            {
                "spans": [
                    {
                        "traceID": "t-1",
                        "spanID": "s-1",
                        "operationName": "GET /charge",
                        "startTime": int(epoch * 1e6),
                        "duration": 900_000,  # 900ms：超过 0.5s 的慢阈值
                    }
                ]
            }
        ],
    }


class _LatencyTieAdapters:
    """两个服务都慢、且都正常 —— 于是并列的机制是 latency_degradation。"""

    def __init__(self) -> None:
        self.profile_calls: list[str] = []
        self.profile_started_at: int = -1
        self.calls: list[str] = []

    def query_service_error_rate(self, service, start, end):
        self.calls.append(service)
        return {"status": "success",
                "data": {"resultType": "matrix", "result": [
                    {"metric": {}, "values": [[(WINDOW_END - timedelta(seconds=30)).timestamp(),
                                               "0.001"]]}]}}

    def query_service_logs(self, service, start, end, filter=None):
        self.calls.append(service)
        return {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        self.calls.append(service)
        return _trace_payload((WINDOW_END - timedelta(seconds=90)).timestamp())

    def query_service_profile(self, service, start, end):
        self.profile_started_at = len(self.calls)
        self.profile_calls.append(service)
        return {"flamegraph": {"names": ["total", f"{service}.hot"], "values": [10, 9]}}

    @property
    def capabilities(self):
        return frozenset({"error_rate", "logs", "traces", "profile"})


class TestProfileIsOnlyFetchedWhenItCanHelp:
    def _context(self, adapters) -> RunContext:
        return RunContext(
            store=EvidenceStore(),
            adapters=adapters,
            budget=BudgetManager(max_tool_calls=40),
            # 兄弟 + 共享下游：相干 → 歧义闸门会触发 → 回路才会进去
            topology=ServiceTopology.from_edges(
                [("checkout-service", "payment-service"),
                 ("checkout-service", "inventory-service"),
                 ("payment-service", "db"),
                 ("inventory-service", "db")]),
            scope_depth=1,
        )

    def test_the_profile_is_requested_after_the_first_pass(self):
        adapters = _LatencyTieAdapters()

        run_investigation(_request(), self._context(adapters))

        # 首轮全部查询跑完之后才轮到 profile（判别轮），不是首轮就付这笔钱
        assert adapters.profile_calls, "判别轮没有请求火焰图"
        assert adapters.profile_started_at >= 9  # 3 服务 × 3 类查询

    def test_only_services_under_a_latency_hypothesis_get_profiled(self):
        adapters = _LatencyTieAdapters()

        run_investigation(_request(), self._context(adapters))

        assert sorted(adapters.profile_calls) == ["inventory-service", "payment-service"]

    def test_a_quiet_run_never_pays_for_a_profile(self):
        class _Quiet(_LatencyTieAdapters):
            def query_service_traces(self, service, start, end, min_duration_ms=None):
                self.calls.append(service)
                return {"traces_considered": 0, "batches": []}

        adapters = _Quiet()

        run_investigation(_request(), self._context(adapters))

        # 没有并列候选就没有判别轮 —— 也就没有人查火焰图
        assert adapters.profile_calls == []
