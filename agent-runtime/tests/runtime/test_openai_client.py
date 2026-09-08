"""真实 LLM 客户端的契约层：显式 token 上限 + 截断可辨识 + 输出可校验。

## 为什么先写测试而不是先写客户端

接 LLM 的坑不在"发 HTTP"，而在四个**协议层**约束。任何一个漏掉，
P1 都会以难以察觉的方式坏掉（aurora-lab 的实测教训）：

1. **必须显式设 max_tokens**。holmes 的文档原话：「Without an explicit limit,
   some providers fall back to small defaults (for example litellm defaults
   Anthropic-family models to 4096), which truncates long answers mid-response」。
   截断发生时工具参数不完整 → 结构校验失败 → 模型看不到原因 → 重试到预算耗尽。
   aurora-lab 实测这一连锁反应花了 11 分钟 / $2。

2. **截断必须与"模型失败"区分**。`finish_reason=length` 意味着答案被切断，
   这不是模型能力问题，是预算配置问题。混为一谈会导致错误的归因和错误的降级。

3. **输出必须过 schema**（已实现）。模型自称 confidence 要在解析期被拒。

4. **网络/限流失败必须是 `ModelUnavailable`**，让上层记 missing_data 继续，
   而不是让整个调查崩掉。
"""
from __future__ import annotations

import json

import pytest

from traceops.runtime.model import (
    ModelClient, ModelRequest, ModelResponse, ModelUnavailable,
    ModelHypothesis,
)


class FakeTransport:
    """记录请求、按脚本返回响应的传输层（不依赖真实网络）。"""

    def __init__(self, *, status=200, body=None, fail=None):
        self.status = status
        self.body = body
        self.fail = fail
        self.requests: list[dict] = []

    def post(self, url, payload, headers=None, timeout=10.0):
        self.requests.append({"url": url, "payload": payload, "headers": headers or {}})
        if self.fail:
            raise self.fail
        return self.status, self.body


def _openai_reply(content: str, *, finish_reason="stop", tokens=(100, 50)):
    return {
        "choices": [{"finish_reason": finish_reason,
                     "message": {"content": content}}],
        "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]},
    }


VALID_CONTENT = json.dumps({
    "hypotheses": [{
        "candidate_service": "redis",
        "fault_type": "pool_exhaustion",
        "failure_mechanism": "pool exhausted",
        "expected_observations": ["error_logs"],
    }]
})


@pytest.fixture
def request_obj():
    return ModelRequest(incident_id="inc-1", service_hint="order-service",
                        evidence_summary={"logs": 3}, allowed_services=["redis", "order-service"])


class TestMaxOutputTokensIsExplicit:
    def test_request_carries_an_explicit_output_limit(self, request_obj):
        """请求对象必须能表达"最多生成多少 token"。

        没有这个字段，客户端就只能靠 provider 默认值 —— 那正是截断的来源。
        """
        assert hasattr(request_obj, "max_output_tokens")
        assert request_obj.max_output_tokens > 0

    def test_default_limit_is_generous_enough(self):
        """默认值不能是 4096 —— 结构化输出（多假设 + 机制解释）会超。"""
        default = ModelRequest(incident_id="i", service_hint="s", evidence_summary={})
        assert default.max_output_tokens >= 8192


class TestResponseExposesTruncation:
    def test_response_can_report_finish_reason(self):
        """响应必须能表达"被截断了"，否则上层无法区分失败类型。"""
        response = ModelResponse(hypotheses=[], input_tokens=1, output_tokens=1,
                                 finish_reason="length")
        assert response.finish_reason == "length"
        assert response.truncated is True

    def test_normal_finish_is_not_truncated(self):
        response = ModelResponse(hypotheses=[], input_tokens=1, output_tokens=1,
                                 finish_reason="stop")
        assert response.truncated is False


class TestClientFailureSemantics:
    def test_network_error_becomes_model_unavailable(self, request_obj):
        from traceops.runtime.openai_client import OpenAICompatibleModel

        model = OpenAICompatibleModel(
            base_url="http://x/v1", model_name="m",
            transport=FakeTransport(fail=OSError("connection refused")))
        with pytest.raises(ModelUnavailable):
            model.propose(request_obj)

    def test_http_429_becomes_model_unavailable(self, request_obj):
        from traceops.runtime.openai_client import OpenAICompatibleModel

        model = OpenAICompatibleModel(
            base_url="http://x/v1", model_name="m",
            transport=FakeTransport(status=429, body={"error": "rate limited"}))
        with pytest.raises(ModelUnavailable, match="429") as exc:
            model.propose(request_obj)
        assert "rate" in str(exc.value).lower() or "429" in str(exc.value)

    def test_invalid_json_content_becomes_model_unavailable(self, request_obj):
        """模型返回非 JSON → 不能让调用方拿到半个对象。"""
        from traceops.runtime.openai_client import OpenAICompatibleModel

        model = OpenAICompatibleModel(
            base_url="http://x/v1", model_name="m",
            transport=FakeTransport(body=_openai_reply("not json at all")))
        with pytest.raises(ModelUnavailable, match="JSON"):
            model.propose(request_obj)

    def test_schema_violation_becomes_model_unavailable(self, request_obj):
        """模型自称 confidence → 契约违规，必须在解析期拒绝。"""
        from traceops.runtime.openai_client import OpenAICompatibleModel

        bad = json.dumps({"hypotheses": [{
            "candidate_service": "redis", "fault_type": "pool_exhaustion",
            "failure_mechanism": "m", "expected_observations": ["error_logs"],
            "confidence": 0.9}]})
        model = OpenAICompatibleModel(
            base_url="http://x/v1", model_name="m",
            transport=FakeTransport(body=_openai_reply(bad)))
        with pytest.raises(ModelUnavailable, match="confidence|contract|schema"):
            model.propose(request_obj)


class TestSuccessfulCall:
    def test_valid_content_becomes_hypotheses(self, request_obj):
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport(body=_openai_reply(VALID_CONTENT))
        model = OpenAICompatibleModel(base_url="http://x/v1", model_name="m",
                                      transport=transport)
        response = model.propose(request_obj)

        assert len(response.hypotheses) == 1
        assert response.hypotheses[0].candidate_service == "redis"
        assert response.finish_reason == "stop"

    def test_tokens_are_reported_from_usage(self, request_obj):
        from traceops.runtime.openai_client import OpenAICompatibleModel

        model = OpenAICompatibleModel(
            base_url="http://x/v1", model_name="m",
            transport=FakeTransport(body=_openai_reply(VALID_CONTENT, tokens=(120, 60))))
        response = model.propose(request_obj)
        assert response.input_tokens == 120
        assert response.output_tokens == 60

    def test_request_body_sends_explicit_max_tokens(self, request_obj):
        """发出去的请求体必须带 max_tokens —— 这是防截断的实际动作。"""
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport(body=_openai_reply(VALID_CONTENT))
        model = OpenAICompatibleModel(base_url="http://x/v1", model_name="m",
                                      transport=transport)
        model.propose(request_obj)
        body = transport.requests[0]["payload"]
        assert body["max_tokens"] == request_obj.max_output_tokens

    def test_endpoint_is_openai_compatible(self, request_obj):
        from traceops.runtime.openai_client import OpenAICompatibleModel

        transport = FakeTransport(body=_openai_reply(VALID_CONTENT))
        model = OpenAICompatibleModel(base_url="http://x/v1", model_name="m",
                                      transport=transport)
        model.propose(request_obj)
        assert transport.requests[0]["url"] == "http://x/v1/chat/completions"


class TestTruncatedResponseIsDistinguishable:
    def test_length_finish_with_unparseable_content_raises_with_reason(self, request_obj):
        """截断导致 JSON 不完整时，错误信息必须提到截断，而不是只说"JSON 坏"。"""
        from traceops.runtime.openai_client import OpenAICompatibleModel

        truncated = VALID_CONTENT[:40]  # 模拟被切断
        transport = FakeTransport(body=_openai_reply(truncated, finish_reason="length"))
        model = OpenAICompatibleModel(base_url="http://x/v1", model_name="m",
                                      transport=transport)
        with pytest.raises(ModelUnavailable) as exc:
            model.propose(request_obj)
        assert "truncat" in str(exc.value).lower() or "length" in str(exc.value).lower()
