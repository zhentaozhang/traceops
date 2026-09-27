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
        ollama_native: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.transport = transport or _UrllibTransport()
        self.timeout = timeout
        # Ollama 原生模式：走 /api/chat 才能关掉 thinking。
        #
        # 实测（qwen3:4b）：`/v1` 开启思考 57.6s / 1791 tokens，
        # 原生 + think=false + format=json 只需 1.6s / 33 tokens —— **35 倍差异**。
        # 而且 `/v1` 上 thinking 会挤占 max_tokens：max_tokens=512 时
        # content 完全为空（全花在思考上）。`reasoning_effort` /
        # `think` / `enable_thinking` 三个参数在 `/v1` 上均无效。
        #
        # 为什么不是默认：真 OpenAI / DeepSeek 端点没有 `think` 概念，
        # 走 /v1 才对。保留两条路径比"猜端点类型"可靠。
        self.ollama_native = ollama_native
        # 温度默认 0：假设生成需要可复现，不应随机发挥
        self.temperature = temperature
        self.name = f"openai-compatible:{model_name}"

    def propose(self, request: ModelRequest) -> ModelResponse:
        if self.ollama_native:
            return self._propose_native(request)

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

    def _propose_native(self, request: ModelRequest) -> ModelResponse:
        """Ollama 原生 `/api/chat`：可关闭 thinking、可强制 JSON。

        请求体与 OpenAI 形状不同（无 `max_tokens`，用 `options.num_predict`；
        响应是 `{message:{content}}` 而非 `{choices:[...]}`），所以单独一条路径。
        """
        payload = {
            "model": self.model_name,
            "stream": False,
            # 关思考：这是 35 倍性能差与 token 预算的全部来源
            "think": False,
            # 传**真实 schema**而不是字符串 "json"。
            #
            # `"json"` 只约束"输出是合法 JSON"，不约束"符合我们的契约" ——
            # 小模型最容易的"合法"答案就是把输入结构原样回吐（实测发生过：
            # 回吐 {allowed_services, evidence_summary, ...}）。
            # 传 schema 对象则让 llama.cpp 的语法解码器在**生成时**就约束形状。
            "format": _hypothesis_schema_for_ollama(),
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
            "options": {
                "temperature": self.temperature,
                "num_predict": request.max_output_tokens,
            },
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            status, body = self.transport.post(
                f"{self.base_url}/api/chat", payload,
                headers=headers, timeout=self.timeout)
        except Exception as exc:
            raise ModelUnavailable(f"model request failed: {exc}") from exc
        if status != 200:
            detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            raise ModelUnavailable(f"model endpoint returned HTTP {status}: {detail[:300]}")

        # 传输层可能返回 dict，也可能返回原始 JSON 字符串（_UrllibTransport 是后者）。
        # 只处理 dict 会把整个响应体当成 content —— 实测直接导致契约违约。
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ModelUnavailable(f"model response is not JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ModelUnavailable(
                f"unexpected native response type: {type(body).__name__}")

        message = body.get("message") or {}
        content = message.get("content") or ""
        done_reason = body.get("done_reason")
        usage = {
            "prompt_tokens": int(body.get("prompt_eval_count") or 0),
            "completion_tokens": int(body.get("eval_count") or 0),
        }
        return self._build_response(
            content, request, done_reason=str(done_reason or "stop"), usage=usage)

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
        return self._build_response(content, request, done_reason=finish_reason,
                                    usage=usage)

    def _build_response(self, content: Any, request: ModelRequest, *,
                        done_reason: str, usage: dict) -> ModelResponse:
        """把"一段可能被截断的 content"变成 ModelResponse（两种端点共用）。"""
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)

        try:
            parsed = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError as exc:
            # 截断的直接后果就是 JSON 不完整 —— 必须说清是截断，而不是说"模型写错了"
            if done_reason == "length":
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
            finish_reason=done_reason,
        )


def _hypothesis_schema_for_ollama() -> dict:
    """把输出契约转成 Ollama `format` 能用的 JSON Schema。

    去掉 `$schema` / `$id`：它们是文档元信息，不是约束，
    有些解码器会因为无法解析 `$schema` 而退化成"任意 JSON"。
    其余原样保留 —— 契约是唯一事实来源，不在这里重新定义一份。
    """
    try:
        from traceops.runtime.model import hypothesis_output_schema

        schema = dict(hypothesis_output_schema())
    except Exception:
        # 契约加载失败时退化成"合法 JSON"而不是让整个调用失败：
        # 解析期的 validate_model_payload 仍会兜底校验。
        return "json"
    schema.pop("$schema", None)
    schema.pop("$id", None)
    schema.pop("title", None)
    schema.pop("description", None)
    return schema


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
