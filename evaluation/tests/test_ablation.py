"""E1–E5 消融实验框架。

## 现状（评测口径已诚实记录）

> P0 只能跑 E3/E4 的"单臂"版本……要形成真正的消融对比，必须先有 LLM 节点（P1），
> 否则 E1/E2/E5 没有可执行内容。这一点不能含糊——把"当前只有一条路径"包装成
> "已做消融"是学术不诚实。

这个判断是对的，但它也意味着**消融的框架一直不存在**：
连"如何定义两个可比的实验臂"都没有代码。

## 框架要解决什么

消融的本质是**只改一个变量**，其余全部固定：

| 实验 | 改变的变量 |
|---|---|
| E1 | 无工具、无证据引擎，模型直接看观测摘要 |
| E2 | 给模型 MCP 工具，但无证据引擎 |
| E3 | 多阶段调查图（确定性节点） |
| E4 | E3 + 证据引擎（内容寻址/校验） |
| E5 | E4 + 假设验证（语义 + 矛盾校验） |

如果 E4 和 E5 之间还顺带改了拓扑深度或预算，那测出来的差异不能归因到
"假设验证"这个机制上 —— 这是消融实验最常见的失效方式。

## 本模块的边界（诚实）

- **定义并锁定实验臂**：每个臂对应一组确定的 `RunContext` 参数
- **校验"只改一个变量"**：臂之间的差异必须恰好是声明的那一个
- **不提供 LLM**：E1/E2 需要真实模型，框架能构造臂但没有模型跑不了。
  这一点在报告里必须显式标注为 `runnable=false`，而不是假装跑过。
"""
from __future__ import annotations

import pytest

from evaluation.ablation import ARMS, AblationArm, arm_config, diff_arms, runnable_arms


class TestArmDefinitions:
    def test_five_arms_exist(self):
        assert set(ARMS) == {"E1", "E2", "E3", "E4", "E5"}

    def test_each_arm_declares_its_variables(self):
        for arm_id, arm in ARMS.items():
            assert arm.arm_id == arm_id
            assert arm.description, f"{arm_id} needs a description"
            assert isinstance(arm.uses_model, bool)
            assert isinstance(arm.uses_tools, bool)
            assert isinstance(arm.uses_evidence_engine, bool)
            assert isinstance(arm.uses_verification, bool)

    def test_progressive_capability(self):
        """E1→E5 能力单调递增：后一臂不应关掉前一臂已开启的能力。

        这不是"设计美学"：如果 E4 关掉了 E2 有的工具，那么 E4 vs E5 的差异
        就无法归因到"假设验证"上。
        """
        order = ["E1", "E2", "E3", "E4", "E5"]
        for previous, current in zip(order, order[1:]):
            prev_arm, curr_arm = ARMS[previous], ARMS[current]
            assert curr_arm.uses_evidence_engine >= prev_arm.uses_evidence_engine
            assert curr_arm.uses_verification >= prev_arm.uses_verification


class TestOnlyOneVariableChanges:
    def test_e3_to_e4_changes_only_the_evidence_engine(self):
        """E4 = E3 + 证据引擎。这一个差异就是全部差异。"""
        diff = diff_arms("E3", "E4")
        assert diff == ["uses_evidence_engine"], (
            f"E3→E4 must change exactly one variable, got {diff}")

    def test_e4_to_e5_changes_only_verification(self):
        """E5 = E4 + 假设验证。"""
        diff = diff_arms("E4", "E5")
        assert diff == ["uses_verification"], f"got {diff}"

    def test_e1_to_e2_changes_only_tools(self):
        diff = diff_arms("E1", "E2")
        assert diff == ["uses_tools"], f"got {diff}"

    def test_adjacent_arms_never_differ_in_more_than_one_variable(self):
        order = ["E1", "E2", "E3", "E4", "E5"]
        for previous, current in zip(order, order[1:]):
            diff = diff_arms(previous, current)
            assert len(diff) <= 1, f"{previous}→{current} changed {diff}"


class TestConfigRendering:
    def test_e3_and_e4_differ_only_in_engine_flag(self):
        """渲染成 RunContext 参数后，差异必须仍然只有一个键。"""
        e3 = arm_config("E3")
        e4 = arm_config("E4")
        # arm_id 是标识符不是能力，比较时排除
        differing = {
            k for k in set(e3) | set(e4)
            if k != "arm_id" and e3.get(k) != e4.get(k)
        }
        assert differing == {"evidence_engine"}, f"got {differing}"

    def test_config_includes_fixed_conditions(self):
        """固定条件必须显式出现在配置里，否则"同一环境"只是口头承诺。"""
        config = arm_config("E4")
        for key in ("model_temperature", "max_tool_calls", "topology_depth", "prompt_version"):
            assert key in config, f"fixed condition {key} missing from arm config"


class TestRunnability:
    def test_arms_needing_a_model_are_marked_not_runnable(self):
        """E1/E2 需要真实模型；没有模型时必须显式不可运行，不能假装跑过。"""
        arms = runnable_arms(has_model=False)
        assert arms == {"E3", "E4", "E5"}, (
            f"without a model only deterministic arms run: got {arms}")

    def test_all_arms_runnable_with_a_model(self):
        assert runnable_arms(has_model=True) == {"E1", "E2", "E3", "E4", "E5"}

    def test_e5_requires_verification_not_a_model(self):
        """E5 是确定性校验器增强，不需要模型 —— 它不该被 has_model 卡住。"""
        assert "E5" in runnable_arms(has_model=False)


class TestArmsActuallyProduceDifferentOutcomes:
    """光定义实验臂不够 —— E4 与 E5 必须真的能产生**不同的诊断结果**。

    否则消融实验测的只是"两套标签"。E5 相对 E4 的增量是
    「假设验证」：语义覆盖率闸门 + 矛盾检测。这两者目前是**硬编码启用**的，
    没有任何开关能关掉 —— 也就是说 E4 这个臂现在跑出来的东西其实就是 E5。
    """

    def test_runcontext_has_switches_for_e5_capabilities(self):
        """RunContext 必须能关闭 E5 的能力，否则 E4 臂不存在。"""
        import inspect

        from traceops.orchestration.graph import RunContext

        fields = set(inspect.signature(RunContext).parameters)
        assert "enable_semantic_gate" in fields, (
            "E4 must be able to run WITHOUT the semantic coverage gate")
        assert "enable_contradiction" in fields, (
            "E4 must be able to run WITHOUT contradiction detection")

    def test_e4_and_e5_differ_in_observable_outcome(self):
        """同一条“部分覆盖”的场景：E5 降级、E4 保持 SUPPORTED。

        这条断言是消融是否有意义的判据：如果两个臂结果相同，
        E5 的增量就没有被测量到。
        """
        from datetime import UTC, datetime, timedelta

        from traceops.domain.models import (
            Evidence, EvidenceType, HypothesisStatus, RootCauseHypothesis,
        )
        from traceops.evidence.store import EvidenceStore
        from traceops.rca.ranking import RankedCandidate
        from traceops.rca.report import build_report
        from traceops.rca.semantic import check_hypothesis

        ts = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
        hypothesis = RootCauseHypothesis(
            investigation_run_id="r", candidate_service="redis",
            fault_type="latency_degradation",      # 期望 slow_span
            failure_mechanism="m", predicted_observations=["slow_span"])
        # 只有错误日志，没有任何慢 span → 覆盖率为 0
        log = Evidence(
            incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.LOG,
            source_id="loki", service_id="redis", observed_at=ts,
            window_start=ts - timedelta(minutes=2), window_end=ts + timedelta(minutes=3),
            query_template_id="qt-error-logs-v1", query_params={},
            normalized_data={"kind": "log", "total_records": 1, "records": [
                {"timestamp": ts.isoformat(), "message": "ERROR redis timeout",
                 "labels": {"level": "error"}}]},
            checksum="")
        store = EvidenceStore()
        store.add(log)
        ranked = [RankedCandidate(hypothesis_id=hypothesis.id, score=0, rank=1,
                                  status=HypothesisStatus.SUPPORTED)]
        checks = {hypothesis.id: check_hypothesis(hypothesis, [log])}

        e5 = build_report(
            investigation_run_id="r", hypotheses=[hypothesis], links=[],
            evidence=[log], ranked=ranked, fault_start=ts, store=store,
            incident_id="i", semantic_checks=checks)
        e4 = build_report(
            investigation_run_id="r", hypotheses=[hypothesis], links=[],
            evidence=[log], ranked=ranked, fault_start=ts, store=store,
            incident_id="i", semantic_checks=None)   # 不传校验 = 没有语义闸门

        assert e5.diagnosis_status == "INCONCLUSIVE"
        assert e4.diagnosis_status == "SUPPORTED"
        assert e5.diagnosis_status != e4.diagnosis_status, (
            "E4 and E5 must produce different diagnoses, or the ablation measures nothing")
