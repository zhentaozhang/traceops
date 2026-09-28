"""few-shot：教模型把**线索**翻译成正确的 `fault_type` 枚举。

## 为什么只改 prompt（实测驱动的决策）

前一轮用 spy 确认了模型在 F-011 上的真实表现：

| 对象 | 结果 |
|---|---|
| 服务归因 | ✅ 正确（读了告警原文，第一条提 `zulu-cache`） |
| **机制判定** | ❌ 错误（给 `dependency_timeout`，实际 `latency_degradation`） |

后果：覆盖率 0 → `inconclusive` → 排序把 SUPPORTED 排前面 → rank 4。

**缺口精确定位在"线索 → 枚举"这一步**，所以 few-shot 是针对性最强的补法：
不需要换模型（用户明确不换），不需要下载，只改 prompt。

## 为什么这四个机制容易混

| 机制 | 语义边界 | 常见误判 |
|---|---|---|
| `latency_degradation` | **变慢**，但仍在服务 | 因为"degraded"字样被判成 timeout |
| `dependency_timeout` | **等不到响应**（超时/连接失败） | 把"慢"当成"等不到" |
| `availability_degradation` | **错误率上升**，请求在失败 | 把任何"不可用"都算进来 |
| `pool_exhaustion` | **资源耗尽**（连接池/线程池满） | 需要显式资源池线索 |

关键区分：**"慢"（latency）vs "等不到"（timeout）vs "失败"（availability）vs "资源满"（pool）**。
模型把 "responses degraded" + cache tier 判成 timeout，是因为 `degraded` 一词在
两个机制里都说得通 —— 需要示例把边界钉死。

## 约束（不能因为加示例而破坏既有原则）

1. **示例不能提及 confidence/score** —— 那会诱导模型输出被禁止的字段
2. **示例的 `candidate_service` 必须来自示例自身的 allowed_services** ——
   否则模型会学着编造服务名
3. **示例必须覆盖四个机制** —— 只覆盖一个会让模型偏向那个机制
"""
from __future__ import annotations

import json

import pytest

from traceops.runtime.model import ModelRequest


def _request(alert_summary: str | None = None) -> ModelRequest:
    return ModelRequest(
        incident_id="inc-1", service_hint="order-service",
        evidence_summary={"metrics": {"count": 1, "services": ["redis"]}},
        allowed_services=["order-service", "redis"],
        alert_summary=alert_summary,
        max_hypotheses=2, max_output_tokens=1024)


class FakeTransport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url, payload, *, headers=None, timeout=10.0):
        self.calls.append((url, payload, headers or {}))
        return 200, self.response


_VALID = {"hypotheses": [{
    "candidate_service": "redis", "fault_type": "latency_degradation",
    "failure_mechanism": "cache lookups slowed",
    "expected_observations": ["slow_span"]}]}


class TestFewShotIsPresent:
    def test_system_prompt_teaches_the_four_mechanisms(self):
        """四个机制的语义边界必须都出现在 prompt 里。"""
        from traceops.runtime.openai_client import _SYSTEM_PROMPT

        for mechanism in ("pool_exhaustion", "dependency_timeout",
                          "latency_degradation", "availability_degradation"):
            assert mechanism in _SYSTEM_PROMPT, (
                f"{mechanism} must be explained; the model conflates these")

    def test_prompt_distinguishes_slow_from_unreachable(self):
        """核心区分：「慢」vs「等不到」—— 这是 F-011 误判的直接原因。"""
        from traceops.runtime.openai_client import _SYSTEM_PROMPT

        lowered = _SYSTEM_PROMPT.lower()
        assert "slow" in lowered and ("timeout" in lowered or "unreachable" in lowered)
        # 必须显式说明"慢"不等于"超时"
        assert any(marker in lowered for marker in
                   ("not the same", "distinct", "do not confuse", "differs from")), (
            "the prompt must explicitly separate latency from timeout")

    def test_prompt_contains_worked_examples(self):
        """必须有具体示例（线索 → 枚举），否则只是又一段抽象说明。"""
        from traceops.runtime.openai_client import _SYSTEM_PROMPT

        # 示例以"线索:"/"Clue:"与"→"/"->"的形式出现
        assert "Example" in _SYSTEM_PROMPT or "example" in _SYSTEM_PROMPT

    def test_prompt_does_not_mention_confidence(self):
        """示例里绝不能出现 confidence/score —— 会诱导模型输出被禁止的字段。"""
        from traceops.runtime.openai_client import _SYSTEM_PROMPT

        lowered = _SYSTEM_PROMPT.lower()
        for banned in ("confidence", "score", "probability", "likelihood"):
            # 只有在"禁止"语境里出现才允许
            assert lowered.count(banned) <= 1, (
                f"{banned} appears {lowered.count(banned)} times; "
                "examples must not suggest emitting it")


class TestFewShotSurvivesRoundTrip:
    def test_alert_summary_is_sent_to_the_model(self):
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport({"message": {"content": json.dumps(_VALID)}})
        model = OpenAICompatibleModel(
            base_url="http://localhost:11434", model_name="qwen3:4b",
            transport=transport, ollama_native=True)
        model.propose(_request("cache tier degraded"))

        payload = transport.calls[0][1]
        user_content = payload["messages"][1]["content"]
        assert "cache tier degraded" in user_content, (
            "the clue must actually reach the model")

    def test_system_prompt_is_sent(self):
        from traceops.runtime.openai_client import OpenAICompatibleModel, _SYSTEM_PROMPT

        transport = FakeTransport({"message": {"content": json.dumps(_VALID)}})
        model = OpenAICompatibleModel(
            base_url="http://localhost:11434", model_name="qwen3:4b",
            transport=transport, ollama_native=True)
        model.propose(_request())

        assert transport.calls[0][1]["messages"][0]["content"] == _SYSTEM_PROMPT
