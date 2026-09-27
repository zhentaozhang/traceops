"""推理型模型的"思考"必须能关掉，否则会吃掉整个 token 预算。

## 缺陷（实测发现）

用 `qwen3:4b`（thinking 模型）走 `/v1/chat/completions`：

```
max_tokens=8192, 普通提问
→ completion_tokens: 1791, 耗时 57.6s
```

而同一模型走原生 `/api/chat` 并设置 `think:false` + `format:"json"`：

```
→ eval_count: 33, 耗时 1.6s, 输出干净的 JSON
```

**35 倍差异**，而且 `/v1` 路径下的思考内容会挤占 `max_tokens` ——
实测 `max_tokens=512` 时 `content` 完全为空（`finish_reason=length`），
因为 512 个 token 全花在思考上。

后果很具体：
- 我们的客户端默认 60s 超时 → **直接 ModelUnavailable**
- 即使不超时，思考消耗的 token 也让 `max_output_tokens=8192` 形同虚设
- 慢 35 倍对一次调查（可能多次调用）是不可接受的

## 为什么不能只靠 `/v1`

我实测过 `/v1` 上的三种常见写法：

| 参数 | 结果 |
|---|---|
| `reasoning_effort: "none"` | 思考没关掉，反而把思考内容漏进 `content` |
| `think: false` | 无效 |
| `enable_thinking: false` | 无效 |

所以必须支持 **Ollama 原生 `/api/chat`**，它明确提供 `think` 参数。

## 设计

`OpenAICompatibleModel` 增加**可选**的原生模式：
`ollama_native=True` 时走 `/api/chat` + `think:false` + `format:"json"`。

为什么是可选而不是替换：
- 真 OpenAI / DeepSeek 端点没有 `think` 概念，走 `/v1` 才对
- 保留两条路径比"猜端点类型"可靠
"""
from __future__ import annotations

import json

import pytest

from traceops.runtime.model import ModelRequest


def _request() -> ModelRequest:
    return ModelRequest(
        incident_id="inc-1", service_hint="order-service",
        evidence_summary={"logs": {"count": 1, "services": ["redis"]}},
        allowed_services=["order-service", "redis"],
        max_hypotheses=2, max_output_tokens=1024)


class FakeTransport:
    """记录请求，返回预置响应。"""

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url, payload, *, headers=None, timeout=10.0):
        self.calls.append((url, payload, headers or {}))
        return 200, self.response


_VALID_BODY = {"hypotheses": [{
    "candidate_service": "redis",
    "candidate_component": "redis-connection-pool",
    "fault_type": "pool_exhaustion",
    "failure_mechanism": "connection pool exhausted",
    "expected_observations": ["error_logs"],
}]}


class TestNativeOllamaMode:
    def test_uses_native_chat_endpoint(self):
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport({"message": {"content": json.dumps(_VALID_BODY)}})
        model = OpenAICompatibleModel(
            base_url="http://localhost:11434", model_name="qwen3:4b",
            transport=transport, ollama_native=True)
        model.propose(_request())

        url = transport.calls[0][0]
        assert "/api/chat" in url, f"native mode must use /api/chat, got {url}"

    def test_disables_thinking(self):
        """必须显式关思考 —— 否则 35 倍慢且 token 全被吃掉。"""
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport({"message": {"content": json.dumps(_VALID_BODY)}})
        model = OpenAICompatibleModel(
            base_url="http://localhost:11434", model_name="qwen3:4b",
            transport=transport, ollama_native=True)
        model.propose(_request())

        payload = transport.calls[0][1]
        assert payload.get("think") is False, (
            f"thinking must be disabled, got think={payload.get('think')!r}")

    def test_requests_schema_constrained_format(self):
        """format 必须是 schema（而不是裸 "json"）—— 见下一个测试的说明。"""
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport({"message": {"content": json.dumps(_VALID_BODY)}})
        model = OpenAICompatibleModel(
            base_url="http://localhost:11434", model_name="qwen3:4b",
            transport=transport, ollama_native=True)
        model.propose(_request())

        payload = transport.calls[0][1]
        fmt = payload.get("format")
        assert fmt, "format must constrain decoding, not be absent"
        assert isinstance(fmt, dict), \
            f"format should be the hypothesis schema, got {fmt!r}"

    def test_no_auth_header_needed_locally(self):
        """本地 Ollama 不需要 Authorization —— 加了也没害处，但不能崩。"""
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport({"message": {"content": json.dumps(_VALID_BODY)}})
        model = OpenAICompatibleModel(
            base_url="http://localhost:11434", model_name="qwen3:4b",
            transport=transport, ollama_native=True)
        model.propose(_request())
        assert transport.calls[0][2] == {"Content-Type": "application/json"}

    def test_parses_native_response_shape(self):
        """原生响应是 {message:{content}}，不是 {choices:[...]} —— 必须正确解析。"""
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport({"message": {"content": json.dumps(_VALID_BODY)}})
        model = OpenAICompatibleModel(
            base_url="http://localhost:11434", model_name="qwen3:4b",
            transport=transport, ollama_native=True)
        response = model.propose(_request())
        assert len(response.hypotheses) == 1
        assert response.hypotheses[0].candidate_service == "redis"


class TestV1ModeStillWorks:
    def test_default_mode_uses_v1_chat_completions(self):
        """默认（非原生）模式必须保持 /v1 行为不变。"""
        from traceops.runtime.openai_client import OpenAICompatibleModel

        body = {"choices": [{"finish_reason": "stop",
                             "message": {"content": json.dumps(_VALID_BODY)}}]}
        transport = FakeTransport(body)
        model = OpenAICompatibleModel(
            base_url="http://localhost:11434/v1", model_name="qwen3:4b",
            transport=transport)
        model.propose(_request())
        assert "/chat/completions" in transport.calls[0][0]
        assert "think" not in transport.calls[0][1]


class TestNativeModeConstrainsTheSchema:
    """`format:"json"` 只保证"是合法 JSON"，**不保证符合我们的 schema**。

    ## 缺陷（实测发现）

    第一次真实调用 `qwen3:4b` 时，模型把**输入结构原样回吐**了：

    ```
    {allowed_services, evidence_summary, incident_id, max_hypotheses, service_hint}
    ```

    然后契约校验正确地拒绝了它（`additionalProperties:false` 生效）。

    这暴露了一个设计缺口：`format:"json"` 只是"输出合法 JSON"，
    而我们需要的是"输出符合特定 schema"。小模型在只给 `json` 约束时，
    最容易的"合法"答案就是复制输入。

    正确做法：把**真实 schema** 传给 `format` ——
    Ollama 原生端点支持传 JSON schema 对象，由解码器在生成时约束。
    """

    def test_format_carries_the_real_schema_not_just_json(self):
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport({"message": {"content": json.dumps(_VALID_BODY)}})
        model = OpenAICompatibleModel(
            base_url="http://localhost:11434", model_name="qwen3:4b",
            transport=transport, ollama_native=True)
        model.propose(_request())

        payload = transport.calls[0][1]
        fmt = payload.get("format")
        assert isinstance(fmt, dict), (
            f"format must be the hypothesis schema, got {fmt!r} — "
            "'json' alone lets the model echo the input")
        assert fmt.get("type") == "object"
        assert "hypotheses" in (fmt.get("properties") or {})
