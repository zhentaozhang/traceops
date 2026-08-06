"""模型客户端接口与预算保护（蓝图 §8.1 / REVISIONS-1.1 R6）。

P0 不接真实 LLM，但接口必须先立住，否则后续接模型会牵动整条管线。

三条约束写死在代码里，不靠"约定"：

1. **模型只能提假设，不能给置信度**：`ModelHypothesis` 没有 confidence 字段，
   对应蓝图的明确禁令（第一版不使用 LLM 自评 confidence）
2. **每次调用必须走预算**：`reserve → call → settle`，没有绕过路径
3. **模型输出必须可校验**：返回结构化对象，自由文本在 `ModelGateway` 层就被丢弃

`DeterministicModel` 是 P0 的默认实现：不调用网络，返回空假设列表，
让上层逻辑"有模型接口但不依赖模型"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from traceops.runtime.budget import BudgetExhausted, BudgetManager


class ModelUnavailable(RuntimeError):
    """模型不可用（超时/限流/凭证失效）。调用方应记入 missing_data，而不是伪造结果。"""


@dataclass(frozen=True)
class ModelHypothesis:
    """模型提出的候选假设。

    **故意没有 confidence 字段**：模型的自我评分不可靠，且会诱导下游把
    不确定的数字当概率使用。排序由确定性规则负责（rca/ranking.py）。
    """

    candidate_service: str
    fault_type: str
    failure_mechanism: str
    candidate_component: str | None = None
    rationale: str = ""
    expected_observations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModelRequest:
    incident_id: str
    service_hint: str
    evidence_summary: dict[str, Any]
    allowed_services: list[str] = field(default_factory=list)
    max_hypotheses: int = 3


@dataclass(frozen=True)
class ModelResponse:
    hypotheses: list[ModelHypothesis]
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ModelClient(Protocol):
    """模型客户端。实现方负责真正的推理；本层不关心用哪个厂商。"""

    name: str

    def propose(self, request: ModelRequest) -> ModelResponse: ...


class DeterministicModel:
    """P0 默认：不调用 LLM，返回空假设（让确定性检测器独占假设生成）。"""

    name = "deterministic-none"

    def propose(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(hypotheses=[], input_tokens=0, output_tokens=0)


class ScriptedModel:
    """测试用：按脚本返回固定假设，用于验证预算/校验/降级路径。"""

    name = "scripted"

    def __init__(self, *, hypotheses: list[ModelHypothesis] | None = None,
                 tokens: tuple[int, int] = (100, 50), fail: bool = False) -> None:
        self._hypotheses = hypotheses or []
        self._tokens = tokens
        self._fail = fail
        self.calls: list[ModelRequest] = []

    def propose(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        if self._fail:
            raise ModelUnavailable("model timed out")
        input_tokens, output_tokens = self._tokens
        return ModelResponse(self._hypotheses[: request.max_hypotheses], input_tokens, output_tokens)


class ModelGateway:
    """模型调用的唯一入口：预算预留 + 结果校验。

    顺序不可颠倒：先 reserve（余额不足则调用根本不发生），调用，再 settle 记账。
    """

    def __init__(self, client: ModelClient, budget: BudgetManager, *,
                 estimate_tokens: int = 2000) -> None:
        self.client = client
        self.budget = budget
        self.estimate_tokens = estimate_tokens

    def propose(self, request: ModelRequest) -> ModelResponse:
        # 允许的候选服务白名单：模型不能凭空提出未在拓扑里的服务
        allowed = set(request.allowed_services)

        reservation = self.budget.reserve(estimate=self.estimate_tokens, purpose="hypothesis_proposal")
        try:
            response = self.client.propose(request)
        except Exception as exc:
            self.budget.release(reservation)  # 调用未发生（或失败），不记账
            raise ModelUnavailable(str(exc)) from exc

        try:
            self.budget.settle(reservation, actual=response.total_tokens)
        except BudgetExhausted:
            raise  # settle 不会抛，这里防御性保留

        validated = self._validate(response, allowed)
        return ModelResponse(
            hypotheses=validated,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

    def _validate(self, response: ModelResponse, allowed: set[str]) -> list[ModelHypothesis]:
        """模型输出必须过白名单：越界假设直接丢弃，不进入排序。"""
        kept: list[ModelHypothesis] = []
        for hypothesis in response.hypotheses:
            if not hypothesis.candidate_service or not hypothesis.fault_type:
                continue
            if allowed and hypothesis.candidate_service not in allowed:
                continue
            kept.append(hypothesis)
        return kept

    def model_info(self) -> dict[str, Any]:
        return {
            "name": self.client.name,
            "estimate_tokens": self.estimate_tokens,
            "budget_used": self.budget.tokens_used,
            "budget_remaining": self.budget.tokens_remaining,
        }


def hypothesis_to_domain(model_hypothesis: ModelHypothesis, *, investigation_run_id: str):
    """模型假设 → 领域假设。状态从 proposed 开始，不带任何"已支持"的暗示。"""
    from traceops.domain.models import HypothesisStatus, RootCauseHypothesis

    return RootCauseHypothesis(
        investigation_run_id=investigation_run_id,
        candidate_service=model_hypothesis.candidate_service,
        candidate_component=model_hypothesis.candidate_component,
        fault_type=model_hypothesis.fault_type,
        failure_mechanism=model_hypothesis.failure_mechanism,
        predicted_observations=list(model_hypothesis.expected_observations),
        status=HypothesisStatus.PROPOSED,
    )
