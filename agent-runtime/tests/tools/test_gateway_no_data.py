"""工具层必须区分「查了，没有数据」与「没法查」。

## 为什么这是地基（holmesgpt `StructuredToolResult.status`）

holmes 的工具结果状态机是
`{success, error, no_data, approval_required, frontend_pause}`，
其中 `no_data` 由「返回码正常但输出为空」判定。

TraceOps 现在只有 `OK / ERROR / REJECTED_SCHEMA / ...`，**没有 NO_DATA**。
后果是两种截然不同的情况在审计层长得一样：

| 真实情况 | 现在的表现 | 应该表现 |
|---|---|---|
| 数据源正常，但这个时间窗真没有日志 | `OK`，然后证据被丢弃 | `NO_DATA`（查过了，确实没有） |
| Loki 挂了 / 凭证失效 | `ERROR` | `ERROR`（没法查） |

这两者对**结论**的意义完全相反：

- 「查了没有」→ 可以支持 `REFUTED`（"如果真是池耗尽，日志里应该有超时，
  但查了没有"）
- 「没法查」→ 只能进 `missing_data`，**必须禁止 REFUTED**（我们什么都没看见）

混为一谈的代价：aurora-lab 记录过 `summarization.py` 的注释——
「without agent_reasoning the summarizer only sees raw tool outputs and
routinely turns absence of evidence into fabricated findings」。
我们现在的形状正是那个 bug 的上游。
"""
from __future__ import annotations

import pytest

from traceops.runtime.budget import BudgetManager
from traceops.tools.gateway import ToolGateway, ToolPermission, ToolSpec


def _gateway() -> tuple[ToolGateway, dict]:
    gw = ToolGateway()
    state: dict[str, str] = {}

    def empty_result(service: str) -> dict:
        state["called"] = service
        # 数据源正常响应，但结果为空 —— 这就是 NO_DATA
        return {"status": "success", "data": {"resultType": "matrix", "result": []}}

    def non_empty(service: str) -> dict:
        return {"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"service": service}, "values": [[1.0, "0.5"]]}]}}

    def raises(service: str) -> dict:
        raise RuntimeError("datasource unreachable")

    for name, impl in (("empty", empty_result), ("nonempty", non_empty), ("raises", raises)):
        gw.register(ToolSpec(
            name=name, description=name,
            input_schema={"type": "object", "properties": {"service": {"type": "string"}},
                          "required": ["service"], "additionalProperties": False},
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=impl,
        ))
    return gw, state


class TestNoDataOutcome:
    def test_empty_result_is_audited_as_no_data(self):
        """数据源正常返回空结果 → 审计 outcome 必须是 NO_DATA，不是 OK。"""
        gw, _ = _gateway()
        gw.invoke("empty", {"service": "redis"}, budget=BudgetManager())

        outcomes = [a["outcome"] for a in gw.audit_log()]
        assert "NO_DATA" in outcomes, f"got {outcomes}"

    def test_non_empty_result_is_still_ok(self):
        gw, _ = _gateway()
        gw.invoke("nonempty", {"service": "redis"}, budget=BudgetManager())
        outcomes = [a["outcome"] for a in gw.audit_log()]
        assert outcomes[-1] == "OK"

    def test_datasource_failure_is_error_not_no_data(self):
        """数据源异常必须是 ERROR：「没法查」与「查了没有」不能混。"""
        gw, _ = _gateway()
        with pytest.raises(RuntimeError):
            gw.invoke("raises", {"service": "redis"}, budget=BudgetManager())
        outcomes = [a["outcome"] for a in gw.audit_log()]
        assert "ERROR" in outcomes and "NO_DATA" not in outcomes

    def test_no_data_is_queryable_from_audit(self):
        """审计必须能直接回答"哪些查询是查过但没数据的"。"""
        gw, _ = _gateway()
        gw.invoke("empty", {"service": "redis"}, budget=BudgetManager())
        rows = gw.audit_records(outcome="NO_DATA")
        assert len(rows) == 1 and rows[0]["tool"] == "empty"


class TestDuplicateCallGuard:
    """同一次调查里，同名+完全相同参数的第二次调用应被拒绝。

    holmes `core/safeguards.py:24 prevent_overly_repeated_tool_call` 的做法。
    aurora-lab 记录过一个 run 把 2/3 预算花在 25 次重复写入上 ——
    预算消耗在重复查询上是真实风险。
    """

    def test_identical_second_call_is_rejected(self):
        gw, state = _gateway()
        budget = BudgetManager(max_tool_calls=10)
        gw.invoke("nonempty", {"service": "redis"}, budget=budget)
        with pytest.raises(Exception) as exc:
            gw.invoke("nonempty", {"service": "redis"}, budget=budget)
        assert "duplicate" in str(exc.value).lower()

    def test_different_params_are_allowed(self):
        gw, _ = _gateway()
        budget = BudgetManager(max_tool_calls=10)
        gw.invoke("nonempty", {"service": "redis"}, budget=budget)
        gw.invoke("nonempty", {"service": "mysql"}, budget=budget)  # 不同参数，允许

    def test_duplicate_does_not_consume_budget(self):
        """重复调用必须**不消耗**预算，否则"防重复"本身成了成本。"""
        gw, _ = _gateway()
        budget = BudgetManager(max_tool_calls=10)
        gw.invoke("nonempty", {"service": "redis"}, budget=budget)
        used = budget.tool_calls
        with pytest.raises(Exception):
            gw.invoke("nonempty", {"service": "redis"}, budget=budget)
        assert budget.tool_calls == used
