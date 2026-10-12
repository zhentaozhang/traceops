"""消融臂必须**真的**作用到运行上，而不只是被算出一个 digest。

## 为什么（这是我实测发现的缺陷）

`evaluation/ablation_runner.py` 每个臂都做了：

```python
profile = arm_profile(arm_id)
print(f"{arm_id}: {ok}/{len(scenarios)} AC@1  (profile={profile.digest()})")
```

**打了 digest，但从来没把它交给 `run_scenario`。** 于是：

- E3、E4、E5 三臂跑的是**同一份 RunContext**
- 三个臂的输出完全相同 —— 这不是"场景没有区分力"，
  而是**三个臂根本没跑不同的东西**

消融实验的全部意义是"只差一个能力位，看指标怎么动"。如果那个能力位
从未被传下去，测出来的是**同一个实验跑了三遍**。这比"测不出差异"更糟：
它会把"装了三遍"伪装成"消融过，E5 没有增益"。

## 逐层要证明的事

1. `run_scenario` 能接收一个 profile
2. 它把 profile 的能力位**真的**写进 `RunContext`
3. `ablation_runner` **真的**把 `arm_profile(arm_id)` 传给 `run_scenario`
   —— 否则前面两条都只是"有能力但没接线"，正是这个仓库反复出现的失败模式
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from evaluation import harness
from evaluation.investigation_profile import DEFAULT_PROFILE


class TestRunScenarioAcceptsAProfile:
    def test_run_scenario_has_a_profile_parameter(self):
        params = set(inspect.signature(harness.run_scenario).parameters)
        assert "profile" in params, (
            "run_scenario 必须能接收 profile，否则消融臂的能力位无处可传")

    def test_the_profile_reaches_the_run_context(self, monkeypatch):
        """把 profile 传进去，RunContext 上的开关必须跟着变。

        这是"接线"的核心断言：算出来的 profile 必须**抵达**执行点。
        只断言参数存在是不够的 —— **参数存在但不被使用**正是本仓库的经典缺陷。
        """
        captured = {}

        class _Stop(Exception):
            """捕获 RunContext 后立刻中断，不需要真跑完七个节点。"""

        def fake_run_investigation(request, ctx, **kwargs):
            captured["ctx"] = ctx
            raise _Stop

        monkeypatch.setattr(harness, "run_investigation", fake_run_investigation)

        from evaluation.ablation import arm_profile

        # E4 = 有证据引擎、无假设验证
        e4 = arm_profile("E4")
        from evaluation.scenario import load_scenarios
        scenario = load_scenarios(Path(__file__).parents[1] / "scenarios")[1]
        with pytest.raises(_Stop):
            harness.run_scenario(scenario, profile=e4)

        ctx = captured["ctx"]
        assert ctx.enable_semantic_gate is False, (
            "E4 不应启用语义闸门，实际 {}" .format(ctx.enable_semantic_gate))

    def test_no_profile_keeps_current_behaviour(self):
        """不传 profile 时行为必须和现在完全一致 —— 默认不能被悄悄改变。"""
        assert DEFAULT_PROFILE.enable_semantic_gate is True
        assert DEFAULT_PROFILE.enable_contradiction is True


class TestAblationRunnerPassesTheProfile:
    def test_each_arm_runs_with_its_own_profile(self, monkeypatch):
        """三个可运行臂必须传入**三个不同**的 profile。

        判据不看 digest 打印，而看实际传进 `run_scenario` 的东西。
        """
        seen: list[str] = []

        def fake_run_scenario(scenario, *, model=None, profile=None):
            seen.append(profile.digest() if profile is not None else "NO-PROFILE")
            return harness.score_scenario(scenario, _empty_result())

        monkeypatch.setattr(
            "evaluation.ablation_runner.run_scenario", fake_run_scenario)

        from evaluation.ablation_runner import main

        scenarios_dir = str(Path(__file__).parents[1] / "scenarios")
        main(["--scenarios", scenarios_dir, "--out", "/tmp/ablation-test.json"])

        passed = [d for d in seen if d != "NO-PROFILE"]
        assert passed, "ablation_runner 从未把 profile 传给 run_scenario"
        assert "NO-PROFILE" not in seen, (
            f"有的臂没有传 profile：{seen}")
        # E3/E4/E5 三个臂，distinct digest 至少要有 2 个（E4/E5 必须不同）
        assert len(set(passed)) >= 2, (
            "E3/E4/E5 传的是同一份 profile —— 消融等于同一实验跑三遍")


def _empty_result():
    from traceops.orchestration.graph import InvestigationResult

    return InvestigationResult(
        status="INCONCLUSIVE", report=None, evidence_ids=[], errors=[],
        tool_calls=0, tokens_used=0, no_data_queries=[])
