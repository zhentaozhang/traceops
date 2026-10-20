"""旋钮声明的三件价值：校验、取值、差异。

这些测试守着同一件事：**声明不能和实现漂移**。
一份"写得很漂亮但没人用"的字段表，比没有字段表更糟 ——
它让人以为配置被校验过了。
"""
from __future__ import annotations

import pytest

from traceops.config.fields import (
    STRATEGY_FIELDS,
    FieldError,
    FieldSpec,
    current_knobs,
    describe_fields,
    diff_knobs,
    validate_knobs,
)


class TestDefaultsComeFromTheImplementation:
    def test_every_declared_default_matches_the_live_value(self):
        """`default` 必须是实现里的值，不是手抄的数字。

        手抄的默认值一定会漂移：有人改了 `ERROR_RATE_THRESHOLD`，
        文档与差异计算还停在旧值，而两处都不会报错。
        """
        live = current_knobs()
        for path, spec in STRATEGY_FIELDS.items():
            assert spec.current() == live[path]
            if spec.path.startswith(("thresholds.", "ranking.")):
                # 这几项有真正的权威源（detectors / ranking 里的常量），
                # 声明里的 default 只是同值快照，必须与读取结果一致。
                assert spec.default == live[path], (
                    f"{path}: declared default {spec.default} != live {live[path]}")

    def test_the_budget_defaults_are_module_constants(self):
        """预算默认值曾是写在构造签名里的字面量，文档引用它时无从对齐。"""
        from traceops.runtime import budget

        assert STRATEGY_FIELDS["budget.deadline_seconds"].current() == budget.DEFAULT_DEADLINE_SECONDS
        assert STRATEGY_FIELDS["budget.max_tool_calls"].current() == budget.DEFAULT_MAX_TOOL_CALLS

    def test_changing_a_live_constant_changes_the_knob_value(self, monkeypatch):
        from traceops.orchestration import detectors

        monkeypatch.setattr(detectors, "ERROR_RATE_THRESHOLD", 0.42)
        assert current_knobs()["thresholds.error_rate"] == 0.42


class TestValidationRejectsWhatWouldSilentlyDoNothing:
    def test_a_percentage_written_as_a_whole_number_is_rejected(self):
        """`error_rate=5` 的本意是 5%，但它永远不触发（>1 的比率不可能）。

        这种错误不会抛异常、不会让测试红，只会让阈值永久失效 ——
        所以必须在入口拒绝。
        """
        with pytest.raises(FieldError) as excinfo:
            validate_knobs({"thresholds.error_rate": 5})
        assert "above the maximum" in str(excinfo.value)
        assert "thresholds.error_rate" in str(excinfo.value)

    def test_a_boolean_is_not_a_number(self):
        """`max_tool_calls=True` 会被当成 1 —— 预算被悄悄掐到 1 次调用。"""
        with pytest.raises(FieldError) as excinfo:
            validate_knobs({"budget.max_tool_calls": True})
        assert "boolean" in str(excinfo.value)

    def test_fractional_value_for_an_integer_field_is_rejected(self):
        with pytest.raises(FieldError):
            validate_knobs({"budget.max_rounds": 2.5})

    def test_a_typo_is_an_error_not_a_no_op(self):
        with pytest.raises(FieldError) as excinfo:
            validate_knobs({"thresholds.errorRate": 0.1})
        assert "unknown configuration field" in str(excinfo.value)
        # 报错要列出可用字段，否则只能去翻源码。
        assert "thresholds.error_rate" in str(excinfo.value)

    def test_valid_overrides_are_accepted_and_merged(self):
        resolved = validate_knobs({"thresholds.error_rate": 0.02})
        assert resolved["thresholds.error_rate"] == 0.02
        # 未覆盖的字段仍然有值：调用方拿到的永远是一份完整配置。
        assert resolved["budget.max_tool_calls"] == current_knobs()["budget.max_tool_calls"]


class TestDiff:
    def test_diff_lists_changed_paths_in_order(self):
        before = current_knobs()
        after = validate_knobs({
            "thresholds.error_rate": 0.1,
            "budget.max_rounds": 5,
        })
        assert diff_knobs(before, after) == [
            "budget.max_rounds", "thresholds.error_rate"]

    def test_identical_configs_have_no_diff(self):
        knobs = current_knobs()
        assert diff_knobs(knobs, dict(knobs)) == []

    def test_a_field_missing_from_history_is_not_reported_as_changed(self):
        """跨版本比较：旧配置里没有这个旋钮 ≠ 值变了。"""
        before = {"thresholds.error_rate": 0.05}
        after = {"thresholds.error_rate": 0.05, "budget.max_rounds": 10}
        assert diff_knobs(before, after) == []


class TestShape:
    def test_field_paths_are_unique_and_dotted(self):
        paths = [spec.path for spec in STRATEGY_FIELDS.values()]
        assert len(paths) == len(set(paths))
        assert all("." in path for path in paths)

    def test_describe_covers_every_field_in_declaration_order(self):
        described = describe_fields()
        assert [entry["path"] for entry in described] == list(STRATEGY_FIELDS)
        for entry in described:
            assert entry["description"], f"{entry['path']} 没有说明"
            assert entry["kind"] in ("number", "integer", "boolean")

    def test_a_spec_with_impossible_bounds_is_rejected(self):
        with pytest.raises(FieldError):
            FieldSpec(path="x.y", kind="number", default=0, description="d",
                      minimum=10, maximum=1)

    def test_an_unknown_kind_is_rejected(self):
        with pytest.raises(FieldError):
            FieldSpec(path="x.y", kind="text", default="", description="d")
