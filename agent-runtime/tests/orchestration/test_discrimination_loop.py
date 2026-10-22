"""并列候选必须被**再问一次**分开，而不是直接弃权。

## 被替换的缺陷

图是直线：`plan → collect → hypothesize → link → correlate → rank → report`。
唯一的条件边只在 `status == "FAILED"` 时改道。于是"两个候选并列"这件事
在 rank 之后**没有任何处理手段**，只能交给 ambiguity_gate 弃权。

弃权是诚实的，但它把"一次额外查询就能分开"的情形也一起放弃了：
首轮查询用的是整个故障窗口（几十分钟），正常波动和真正的起变点混在一起，
两个候选看起来一样坏。而"谁先开始坏、谁在起变点那一刻就已经坏了"
恰恰是能把它俩分开的那件事 —— 只要把窗口收到起变点附近再问一次。

## 这条回路的三条纪律

1. **只在必要时花钱**：仅当首名**分数完全相同且落在不同服务上**才进回路。
   差 1 分已经意味着"有一条证据支持 A 而 B 没有"，那是可判别的差异。
2. **有界**：`DISCRIMINATION_ROUNDS = 2`，且计划按剩余工具调用数截断。
   没有上限的自我改进回路是烧钱机器。
3. **不许原地打转**：`discriminated` 记住问过哪些服务，`_discriminate_node`
   在计划为空时直接报错，而不是静默重跑旧计划。

## 判据不是新造的

"聚焦窗口里该服务错误率全程正常"这条观测，会被既有的
`traceops/rca/contradiction.py` 判成 `CONTRADICTS`（R-005 降权）。
所以回路不需要新的评分规则 —— 它只是**创造条件让既有规则拿到证据**。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from traceops.evidence.store import EvidenceStore
from traceops.orchestration.graph import (
    CTX_CONFIG_KEY,
    InvestigationRequest,
    RunContext,
    _discriminate_node,
    _focused_window,
    run_investigation,
)
from traceops.runtime.budget import BudgetManager
from traceops.topology.graph import ServiceTopology

TS = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
WINDOW_START = TS - timedelta(minutes=2)
WINDOW_END = TS + timedelta(minutes=3)

#: 聚焦窗口半径（与 graph.FOCUSED_WINDOW_SECONDS 同值）。
#: 用它区分"首轮整窗查询"与"判别轮聚焦查询"：断言里不写魔法数字。
FOCUSED = timedelta(seconds=60)


def _metric(*, value: float, last_epoch: float) -> dict:
    """一条单个采样点的 Prometheus matrix。

    采样点必须落在请求窗口内、且不晚于"现在" —— 否则会被 `Evidence`
    的时间不变量拒绝，那个失败会表现为 missing_data 而不是"查询没返回"。
    """
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"service": "s"},
                    "values": [[last_epoch, str(value)]],
                }
            ],
        },
    }


class _TiedAdapters:
    """首轮 payment 与 inventory **同样坏、且最后一个采样点同一时刻** → 分数完全相同。

    这两个服务是兄弟节点（都挂在 checkout 下），彼此之间没有依赖边 ——
    所以 R-008（"某服务的症状，不是根因"）无法把它们分开，这正是歧义闸门
    真正会触发的情形。checkout 自己正常（0.001），因此不是候选。

    判别轮（聚焦窗口）只让 payment 异常，inventory 恢复正常 ——
    "inventory 在那一段时间其实没坏"就是推翻它的那条观测。
    """

    def __init__(self) -> None:
        self.focused_calls: list[str] = []
        self.coarse_calls: list[str] = []

    def query_service_error_rate(self, service, start, end):
        start_at = datetime.fromisoformat(start)
        end_at = datetime.fromisoformat(end)
        focused = (end_at - start_at) <= 2 * FOCUSED + timedelta(seconds=1)
        last = (end_at - timedelta(seconds=30)).timestamp()
        if focused:
            self.focused_calls.append(service)
            value = 0.42 if service == "payment-service" else 0.001
        else:
            self.coarse_calls.append(service)
            value = 0.001 if service == "checkout-service" else 0.42
        return _metric(value=value, last_epoch=last)

    def query_service_logs(self, service, start, end, filter=None):
        return {"status": "success", "data": {"resultType": "streams", "result": []}}

    def query_service_traces(self, service, start, end, min_duration_ms=None):
        return {"traces_considered": 0, "batches": []}


def _request() -> InvestigationRequest:
    return InvestigationRequest(
        investigation_run_id="run-discriminate",
        incident_id="inc-discriminate",
        environment="demo",
        service_hint="checkout-service",
        fault_start=TS,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
    )


def _context(adapters, *, max_tool_calls: int = 40) -> RunContext:
    return RunContext(
        store=EvidenceStore(),
        adapters=adapters,
        # scope_depth=1：起点 + 直接下游，payment 与 inventory 都在 investigation 层。
        #
        # 两者必须是"相干"的（这里共享同一个下游 db）：否则
        # `report._independent_faults` 会判成"两个互不相干的独立故障"，
        # 歧义闸门直接放行、根本不会弃权 —— 也就测不到回路。
        # 同时它们之间没有直接的上下游边，所以 R-008（"某服务的症状"）
        # 也不会替我们分开它们。
        budget=BudgetManager(max_tool_calls=max_tool_calls),
        topology=ServiceTopology.from_edges(
            [("checkout-service", "payment-service"),
             ("checkout-service", "inventory-service"),
             ("payment-service", "db"),
             ("inventory-service", "db")]),
        scope_depth=1,
    )


# ---------- 1. 回路真的把并列分开了 ----------

class TestATieIsBrokenByOneMoreRound:
    def test_the_loop_probes_exactly_the_tied_services(self):
        adapters = _TiedAdapters()

        run_investigation(_request(), _context(adapters))

        # 两个并列服务各问一次，且**只问一次**（问第二遍同样的查询只是烧钱）
        assert sorted(adapters.focused_calls) == ["inventory-service", "payment-service"]

    def test_the_second_round_changes_the_diagnosis(self):
        adapters = _TiedAdapters()

        result = run_investigation(_request(), _context(adapters))

        # 首轮这里必然是"两个等价候选"→ 弃权；多问一趟之后应当给出定位
        assert result.report.diagnosis_status == "SUPPORTED"
        assert "payment-service" in (result.report.summary or "")

    def test_evidence_from_both_rounds_survives(self):
        adapters = _TiedAdapters()

        result = run_investigation(_request(), _context(adapters))

        # 无 reducer 的 state 键是"后写覆盖"：第二趟会让第一趟的证据凭空消失，
        # 而排序要靠全部证据，丢掉首轮等于重算了一个更贫乏的世界
        # 首轮 3 条（checkout 正常也是一条证据）+ 判别轮 2 条
        assert len(result.report.evidence) == 5

    def test_a_tie_that_cannot_be_broken_still_abstains(self):
        class _StillTied(_TiedAdapters):
            def query_service_error_rate(self, service, start, end):
                start_at = datetime.fromisoformat(start)
                end_at = datetime.fromisoformat(end)
                focused = (end_at - start_at) <= 2 * FOCUSED + timedelta(seconds=1)
                (self.focused_calls if focused else self.coarse_calls).append(service)
                return _metric(value=0.42, last_epoch=(WINDOW_END - timedelta(seconds=30)).timestamp())

        result = run_investigation(_request(), _context(_StillTied()))

        # 问过了还是分不开 —— 那就还是弃权。回路买的是"确定性"，不是"答案"
        assert result.report.diagnosis_status == "INCONCLUSIVE"


# ---------- 2. 有界：预算不够就不进回路 ----------

class TestTheLoopRespectsTheBudget:
    def test_no_budget_left_means_no_extra_round(self):
        adapters = _TiedAdapters()

        result = run_investigation(_request(), _context(adapters, max_tool_calls=9))

        # 首轮 3 服务 × 3 类查询 = 9 次，正好用完；再问就等于超预算
        assert adapters.focused_calls == []
        assert result.report.diagnosis_status == "INCONCLUSIVE"

    def test_the_round_cap_is_enforced_by_the_node_contract(self):
        # 计划为空时进入判别节点 = 两个键被写坏了。
        # 静默重跑旧计划会重复取证，原地打转更难查 —— 大声报错。
        ctx = _context(_TiedAdapters())
        with pytest.raises(RuntimeError):
            _discriminate_node({"next_plan": [], "round": 0},
                               {"configurable": {CTX_CONFIG_KEY: ctx}})


# ---------- 3. 聚焦窗口必须是请求窗口的子集 ----------

class TestFocusedWindow:
    def test_the_window_is_centred_on_the_earliest_anomaly(self):
        anchor = TS + timedelta(minutes=1)

        start, end = _focused_window(_request(), anchor)

        assert datetime.fromisoformat(start) == anchor - FOCUSED
        assert datetime.fromisoformat(end) == anchor + FOCUSED

    def test_the_window_never_leaves_the_requested_range(self):
        # 异常点贴着窗口左界：向左扩出去就是"查了我没被授权的那段时间"
        start, end = _focused_window(_request(), WINDOW_START + timedelta(seconds=5))

        assert datetime.fromisoformat(start) >= WINDOW_START
        assert datetime.fromisoformat(end) <= WINDOW_END

    def test_no_anchor_falls_back_to_the_fault_start(self):
        start, end = _focused_window(_request(), None)

        assert datetime.fromisoformat(start) == max(TS - FOCUSED, WINDOW_START)
        assert datetime.fromisoformat(end) == min(TS + FOCUSED, WINDOW_END)
