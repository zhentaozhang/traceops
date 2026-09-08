"""OpenAI 兼容的模型客户端（P1 第一个真实 ModelClient 实现）。

## 为什么不引入 openai SDK

`pyproject.toml` 刻意只依赖标准库 + pydantic + jsonschema + langgraph。
OpenAI 兼容端点就是 `POST {base_url}/chat/completions`，用 urllib 发一个请求
足够了；引入 SDK 只会带来依赖升级与隐式默认值（尤其是 `max_tokens` 的默认）。

## 四个协议约束（每个都有测试锁定）

1. **显式 max_tokens**：请求体必须带 `ModelRequest.max_output_tokens`。
   不给的话 provider 会退到很小默认值，结构化输出被截断。
2. **截断可辨识**：`finish_reason=length` 时要让上层知道是"被切断"，
   而不是"模型失败"。错误信息里会明确提到截断。
3. **输出过 schema**：模型自称 confidence 之类的越界字段在解析期被拒
   （复用 `runtime/model.py` 的 `validate_model_payload`）。
4. **失败一律 `ModelUnavailable`**：网络错误 / 429 / 非 JSON 都转成同一个
   异常类型，让编排层记 missing_data 继续，而不是整个调查崩掉。
   唯一的例外是**契约违规**（`ModelContractViolation`），因为那说明
   模型没守住输出格式，与"这次猜得不对"是不同性质的问题。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from traceops.runtime.model import (
    ModelContractViolation,
    ModelHypothesis,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    validate_model_payload,
)

_SYSTEM_PROMPT = """You are the hypothesis generator of an evidence-driven RCA system.

Hard rules:
- You propose ROOT CAUSE CANDIDATES only. You never decide ranking.
- You MUST NOT output any confidence/score/probability/likelihood field.
  Ordering is decided by deterministic rules downstream.
- candidate_service MUST be one of the allowed services given to you.
- fault_type MUST be one of the enumerated mechanisms.
- expected_observations MUST list what you predict will be observed; it is
  cross-checked against collected evidence, so do not list things you cannot
  verify.
- If the evidence is insufficient, return an empty hypotheses list. Abstaining
  is a valid answer; inventing a cause is not.

Respond with JSON only, matching the provided schema."""


class OpenAICompatibleModel:
    """调用 OpenAI 兼容的 /chat/completions。"""

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        api_key: str | None = None,
        transport: Any | None = None,
        timeout: float = 60.0,
        temperature: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.transport = transport or _UrllibTransport()
        self.timeout = timeout
        # 温度默认 0：假设生成需要可复现，不应随机发挥
        self.temperature = temperature
        self.name = f"openai-compatible:{model_name}"

    def propose(self, request: ModelRequest) -> ModelResponse:
        payload = {
            "model": self.model_name,
            "temperature": self.temperature,
            # 显式上限：不给就等着被截断
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({
                    "incident_id": request.incident_id,
                    "service_hint": request.service_hint,
                    "allowed_services": request.allowed_services,
                    "max_hypotheses": request.max_hypotheses,
                    "evidence_summary": request.evidence_summary,
                }, ensure_ascii=False)},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            status, body = self.transport.post(
                f"{self.base_url}/chat/completions", payload,
                headers=headers, timeout=self.timeout)
        except Exception as exc:
            raise ModelUnavailable(f"model request failed: {exc}") from exc

        if status != 200:
            detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            raise ModelUnavailable(f"model endpoint returned HTTP {status}: {detail[:300]}")

        return self._parse(body, request)

    def _parse(self, body: Any, request: ModelRequest) -> ModelResponse:
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ModelUnavailable(f"model response is not JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ModelUnavailable(f"unexpected model response type: {type(body).__name__}")

        choices = body.get("choices") or []
        if not choices:
            raise ModelUnavailable("model response has no choices")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        finish_reason = str(choice.get("finish_reason") or "stop")
        content = (choice.get("message") or {}).get("content") or ""

        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)

        try:
            parsed = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError as exc:
            # 截断的直接后果就是 JSON 不完整 —— 必须说清是截断，不是模型的错
            if finish_reason == "length":
                raise ModelUnavailable(
                    f"model output was truncated at max_tokens={request.max_output_tokens} "
                    f"(finish_reason=length), so its JSON is incomplete: {exc}") from exc
            raise ModelUnavailable(f"model output is not valid JSON: {exc}") from exc

        errors = validate_model_payload(parsed)
        if errors:
            raise ModelContractViolation(
                "model output violates the hypothesis contract: " + "; ".join(errors[:3]))

        hypotheses = [
            ModelHypothesis(
                candidate_service=str(item.get("candidate_service") or ""),
                fault_type=str(item.get("fault_type") or ""),
                failure_mechanism=str(item.get("failure_mechanism") or ""),
                candidate_component=item.get("candidate_component"),
                rationale=str(item.get("rationale") or ""),
                expected_observations=list(item.get("expected_observations") or []),
            )
            for item in (parsed.get("hypotheses") or [])
        ]
        return ModelResponse(
            hypotheses=hypotheses[: request.max_hypotheses],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
        )


class _UrllibTransport:
    """默认传输层：标准库，无第三方依赖。"""

    def post(self, url: str, payload: dict, *, headers: dict | None = None,
             timeout: float = 10.0) -> tuple[int, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=dict(headers or {}),
                                          method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            return exc.code, body
