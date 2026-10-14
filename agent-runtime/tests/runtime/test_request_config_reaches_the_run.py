"""请求里的配置必须真的到达运行现场。

这个文件针对的是一类很难被发现的缺陷：**字段存在，但没人读它**。

`contracts/events/v1/investigation-requested.schema.json` 里早就有 budget、
scope_depth、topology_edges，Java 侧也一直在发；`ModelRequest.alert_summary`
也早就存在，注释写着"规则路径不读它，只透传给模型"。但整条链上没有一环
把它们接起来：

- 契约里没有 alert_summary，于是 Java 发不出来，Python 也读不到；
- `build_context(request)` 收到了 request 却只用它做了一次预算构造，
  `budget`/`scope_depth`/`topology_edges` 全部被忽略。

结果是"评测里能测出自然语言线索的价值，生产里这条线索从来没到过模型"。
一个字段被两端同时忽略，比它不存在于任何一端更难发现 —— 因为 grep 得到
的是"有这个字段"，而不是"没有人用它"。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from traceops.cli.worker import _build_budget, _topology_for, build_run_context
from traceops.orchestration.graph import InvestigationRequest, RunContext
from traceops.runtime.budget import DEFAULT_MAX_ROUNDS, DEFAULT_MAX_TOKENS
from traceops.topology.graph import ServiceTopology, TopologyError


def _args(**overrides):
    base = dict(
        max_tool_calls=50,
        deadline_seconds=900,
        model_url=None,
        model_name=None,
        model_api_key=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _request(**overrides) -> InvestigationRequest:
    payload = dict(
        investigation_run_id="run-001",
        incident_id="inc-001",
        service_hint="order-service",
        fault_start="2026-10-30T10:05:00Z",
        window_start="2026-10-30T10:00:00Z",
        window_end="2026-10-30T10:06:00Z",
    )
    payload.update(overrides)
    return InvestigationRequest(**payload)


class TestBudgetFromTheRequest:
    def test_the_request_can_lower_the_tool_call_ceiling(self):
        budget = _build_budget(_args(max_tool_calls=50), {"max_tool_calls": 7})
        assert budget.max_tool_calls == 7

    def test_the_request_cannot_raise_the_operator_ceiling(self):
        """运维上限是资源保护，控制面的业务判断不能把它顶开。"""
        budget = _build_budget(_args(max_tool_calls=50), {"max_tool_calls": 500})
        assert budget.max_tool_calls == 50

    def test_the_request_can_lower_the_deadline(self):
        budget = _build_budget(_args(deadline_seconds=900), {"deadline_seconds": 60})
        assert budget.deadline_seconds == 60

    def test_a_request_without_budget_falls_back_to_the_operator_ceiling(self):
        budget = _build_budget(_args(max_tool_calls=13), None)
        assert budget.max_tool_calls == 13
        assert budget.max_rounds == DEFAULT_MAX_ROUNDS
        assert budget.max_tokens == DEFAULT_MAX_TOKENS

    @pytest.mark.parametrize("bad", [{"max_tool_calls": "many"}, {"max_tool_calls": None},
                                     {"max_tool_calls": True}, {}])
    def test_garbage_in_the_budget_does_not_silently_become_zero(self, bad):
        """坏值退回到运维上限，而不是退回到 0 —— 0 会把调查直接掐死。"""
        budget = _build_budget(_args(max_tool_calls=50), bad)
        assert budget.max_tool_calls == 50


class TestTopologyFromTheRequest:
    def test_request_edges_win_over_the_static_file(self):
        fallback = ServiceTopology.from_edges([("a", "b")])
        chosen = _topology_for(_request(topology_edges=[["order-service", "redis"]]), fallback)
        assert chosen is not None
        assert chosen.dependencies("order-service") == {"redis"}

    def test_absent_edges_fall_back_to_the_static_file(self):
        fallback = ServiceTopology.from_edges([("a", "b")])
        assert _topology_for(_request(topology_edges=None), fallback) is fallback

    def test_an_empty_list_means_do_not_expand_not_use_everything(self):
        """空列表是控制面明确说"这次不展开依赖"。

        把它和"没提供"混为一谈，就会把"只查单服务"静默改写成"查全量拓扑" ——
        两种行为的采集范围差一个数量级，而日志里看不出来发生过替换。
        """
        fallback = ServiceTopology.from_edges([("a", "b")])
        chosen = _topology_for(_request(topology_edges=[]), fallback)
        assert chosen is not None
        assert chosen.dependencies("a") == set()

    def test_a_self_edge_is_rejected_rather_than_swallowed(self):
        with pytest.raises(TopologyError, match="topology_edges"):
            _topology_for(_request(topology_edges=[["a", "a"]]), None)


class TestScopeDepthReachesTheContext:
    def test_the_request_depth_is_used(self):
        context = build_run_context(_request(scope_depth=0), args=_args(),
                                    store=None, adapters=None, topology=None)
        assert context.scope_depth == 0

    def test_absent_depth_keeps_the_default(self):
        context = build_run_context(_request(), args=_args(),
                                    store=None, adapters=None, topology=None)
        assert context.scope_depth == RunContext.scope_depth

    def test_zero_is_not_the_same_as_absent(self):
        """scope_depth=0 是"只查 service_hint 自身"，不是"用默认值"。"""
        context = build_run_context(_request(scope_depth=0), args=_args(),
                                    store=None, adapters=None, topology=None)
        default = build_run_context(_request(scope_depth=None), args=_args(),
                                    store=None, adapters=None, topology=None)
        assert context.scope_depth != default.scope_depth
