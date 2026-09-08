"""模型输出必须过 schema 校验，且**显式禁止 confidence**。

## 为什么需要 schema 而不是只靠 dataclass

`ModelGateway._validate` 现在是"构造 dataclass → 白名单过滤"。这条路有两个洞：

1. `ModelHypothesis` 是 frozen dataclass，构造时**未知字段会被 TypeError**，
   但那发生在解析之后 —— 模型实际返回了什么，只有它自己知道。
2. 更关键：**confidence 字段**。领域设计明令禁止模型自评置信度
   （`runtime/model.py` 注释写死），但代码里没有任何地方**强制**这一点。
   模型若返回 `{"confidence": 0.9}`，dataclass 会直接报错——错误信息对
   上游是不透明的。

schema 把约束前移到**解析期**：`additionalProperties: false` +
显式字段清单，任何越界字段立刻被拒，错误可读。

## 这是接 LLM 前的必要准备

P1 一旦接真实模型，输出不可控性就进来了。没有 schema 的话，
"模型给了 confidence 所以下游把它当概率用"是迟早的事。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "contracts" / "models" / "hypothesis-output.v1.schema.json"


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator(schema):
    return Draft202012Validator(schema)


def _valid_output(**overrides):
    item = {
        "candidate_service": "redis",
        "fault_type": "pool_exhaustion",
        "failure_mechanism": "connection pool exhausted after maxPoolSize change",
        "expected_observations": ["error_rate_rise", "error_logs"],
    }
    item.update(overrides)
    return {"hypotheses": [item]}


class TestValidOutputAccepted:
    def test_minimal_valid_output(self, validator):
        assert validator.is_valid(_valid_output())

    def test_null_component_allowed(self, validator):
        assert validator.is_valid(_valid_output(candidate_component=None))

    def test_rationale_optional(self, validator):
        assert validator.is_valid(_valid_output(rationale="config change precedes fault"))


class TestConfidenceIsRejected:
    """这是整个 schema 存在的首要原因。"""

    def test_confidence_field_rejected(self, validator):
        assert not validator.is_valid(_valid_output(confidence=0.9)), (
            "confidence must be rejected at parse time; the ranking layer is "
            "the only thing allowed to decide ordering")

    def test_confidence_variants_all_rejected(self, validator):
        for field in ("confidence", "score", "probability", "likelihood"):
            assert not validator.is_valid(_valid_output(**{field: 0.5})), field

    def test_error_message_names_the_offending_field(self, validator):
        """拒绝必须可读：模型/调用方要知道『怎么改』。"""
        errors = list(validator.iter_errors(_valid_output(confidence=0.9)))
        message = " ".join(e.message for e in errors)
        assert "confidence" in message or "additional" in message.lower()


class TestStructuralRejection:
    def test_empty_hypotheses_list_is_valid_shape(self, validator):
        """空列表是合法的形状（模型没提任何假设）—— 由上层决定是否降级。"""
        assert validator.is_valid({"hypotheses": []})

    def test_missing_required_field_rejected(self, validator):
        bad = _valid_output()
        del bad["hypotheses"][0]["failure_mechanism"]
        assert not validator.is_valid(bad)

    def test_empty_mechanism_rejected(self, validator):
        assert not validator.is_valid(_valid_output(failure_mechanism=""))

    def test_unknown_fault_type_rejected(self, validator):
        assert not validator.is_valid(_valid_output(fault_type="cosmic_rays"))

    def test_empty_expected_observations_rejected(self, validator):
        """没有预期观测 → 语义校验无从比对，等于放弃验证能力。"""
        assert not validator.is_valid(_valid_output(expected_observations=[]))

    def test_more_than_three_hypotheses_rejected(self, validator):
        single = _valid_output()["hypotheses"][0]
        assert not validator.is_valid({"hypotheses": [single] * 4})
