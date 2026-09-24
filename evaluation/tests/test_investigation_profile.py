"""InvestigationProfile：把散在 4 处的调查策略打包成**不可变、可 digest 的对象**。

## 为什么需要（不是重构洁癖，是三个真实缺口）

当前调查策略分散在四个文件里：

| 位置 | 内容 |
|---|---|
| `orchestration/graph.py:_PLAN_TEMPLATES` | 采集哪些查询模板 |
| `orchestration/detectors.py:ERROR_RATE_THRESHOLD` 等 | 异常判定阈值 |
| `orchestration/graph.py` | scope_depth / impact_scope 采集策略 |
| `rca/ranking.py:_W_*` | 排序权重 |

这带来三个具体问题：

**1. 消融无法机器归因。** `ablation.diff_arms` 只能比较我**声明**的能力位。
我说"E4 与 E5 只差语义闸门"，但没有任何东西能证明阈值、模板、权重也一致。
一旦有人调了 `ERROR_RATE_THRESHOLD`，E4/E5 的差异就不再可归因 —— 而**没有人会发现**。

**2. 多轮结论无法归因。** `FindingHistorySnapshot` 存了结论，但**没存策略**。
同一个事故两次调查结论不同时，无法回答"是策略变了还是数据变了"。

**3. 调阈值没有安全入口。** SRE 想调 `ERROR_RATE_THRESHOLD`，
现在只能改源码重新部署 —— 没有记录、没有版本、无法回滚。

## 设计要点

- **frozen dataclass + `digest()`**：策略内容是哈希，可直接进报告与历史快照
- **digest 只覆盖语义内容**，不含构造时间/来源路径（否则同样的策略会得到不同 digest）
- **与现状等价**：默认 profile 必须复现当前行为，不能因为引入抽象而改变结论

## 边界

- 不引入配置热加载、不做 YAML：profile 是**代码内的不可变对象**，
  加载/持久化是它之后的事
- 不改变任何现有阈值 —— 这轮只是把散落的值集中，并让它们可被证明一致
"""
from __future__ import annotations

import json

import pytest

from evaluation.investigation_profile import InvestigationProfile, DEFAULT_PROFILE


class TestProfileIsImmutableAndDigestable:
    def test_profile_is_frozen(self):
        with pytest.raises(Exception):
            DEFAULT_PROFILE.error_rate_threshold = 0.9

    def test_digest_is_stable_across_instances(self):
        """同样的策略内容 → 同样的 digest（否则无法比对）。"""
        a = InvestigationProfile()
        b = InvestigationProfile()
        assert a.digest() == b.digest()

    def test_digest_changes_when_any_threshold_changes(self):
        from dataclasses import replace

        changed = replace(DEFAULT_PROFILE, error_rate_threshold=0.06)
        assert changed.digest() != DEFAULT_PROFILE.digest(), (
            "a threshold change must change the digest, or attribution silently breaks")

    def test_digest_changes_when_a_weight_changes(self):
        from dataclasses import replace

        changed = replace(DEFAULT_PROFILE, w_contradiction=-4)
        assert changed.digest() != DEFAULT_PROFILE.digest()

    def test_digest_changes_when_templates_change(self):
        from dataclasses import replace

        changed = replace(DEFAULT_PROFILE,
                          plan_templates=DEFAULT_PROFILE.plan_templates[:-1])
        assert changed.digest() != DEFAULT_PROFILE.digest()

    def test_digest_excludes_non_semantic_metadata(self):
        """digest 不能包含"谁构造的/什么时候"这类东西。"""
        profile = InvestigationProfile()
        assert "generated" not in json.dumps(profile.to_dict())
        assert "path" not in json.dumps(profile.to_dict())


class TestDefaultProfileReproducesCurrentBehaviour:
    """引入抽象不能改变行为 —— 默认值必须与代码里现有常量一致。"""

    def test_thresholds_match_the_code(self):
        from traceops.orchestration.detectors import (
            ERROR_RATE_THRESHOLD, SLOW_SPAN_THRESHOLD_SECONDS,
        )
        assert DEFAULT_PROFILE.error_rate_threshold == ERROR_RATE_THRESHOLD
        assert DEFAULT_PROFILE.slow_span_threshold_seconds == SLOW_SPAN_THRESHOLD_SECONDS

    def test_ranking_weights_match_the_code(self):
        from traceops.rca import ranking
        assert DEFAULT_PROFILE.w_demote == ranking._W_DEMOTE
        assert DEFAULT_PROFILE.w_anomaly_before == ranking._W_ANOMALY_BEFORE
        assert DEFAULT_PROFILE.w_trace_anomaly == ranking._W_TRACE_ANOMALY
        assert DEFAULT_PROFILE.w_config_change == ranking._W_CONFIG_CHANGE
        assert DEFAULT_PROFILE.w_contradiction == ranking._W_CONTRADICTION
        assert DEFAULT_PROFILE.w_component_specific == ranking._W_COMPONENT_SPECIFIC

    def test_templates_match_the_code(self):
        from traceops.orchestration.graph import _PLAN_TEMPLATES
        assert len(DEFAULT_PROFILE.plan_templates) == len(_PLAN_TEMPLATES)
        for profile_tpl, code_tpl in zip(DEFAULT_PROFILE.plan_templates, _PLAN_TEMPLATES):
            assert profile_tpl.tool_name == code_tpl[0]
            assert profile_tpl.source_id == code_tpl[1]
            assert profile_tpl.template_id == code_tpl[3]


class TestProfileEnablesMachineProvenAttribution:
    """核心价值：消融的"只改一个变量"从**声明**变成**可证明**。"""

    def test_two_arms_with_identical_profiles_prove_they_are_comparable(self):
        a = InvestigationProfile()
        b = InvestigationProfile()
        assert a.digest() == b.digest(), "identical strategy must be provable identical"

    def test_a_quiet_threshold_change_is_now_visible(self):
        """有人偷偷调了阈值 —— digest 会变，消融的归因随之失效。

        这正是当前缺失的保护：没有 digest 时，这种改动**完全不可见**。
        """
        from dataclasses import replace

        tampered = replace(DEFAULT_PROFILE, error_rate_threshold=0.03)
        assert tampered.digest() != DEFAULT_PROFILE.digest()


class TestProfileSerialisesIntoReports:
    def test_to_dict_is_json_safe(self):
        payload = DEFAULT_PROFILE.to_dict()
        assert json.dumps(payload)  # 不抛 = 可进事件/报告

    def test_digest_is_short_and_hex(self):
        digest = DEFAULT_PROFILE.digest()
        assert len(digest) <= 64
        int(digest, 16)  # 合法 hex


class TestProfileReachesReportsAndHistory:
    """digest 必须真的进入报告与历史快照 —— 否则"可归因"只是空话。

    这正是缺口 2：`FindingHistorySnapshot` 存了结论但没存策略，
    所以两次调查结论不同时无法回答"是策略变了还是数据变了"。
    """

    def test_report_carries_the_profile_digest(self):
        from datetime import UTC, datetime, timedelta

        from traceops.domain.models import Evidence, EvidenceType, HypothesisStatus, RootCauseHypothesis
        from traceops.evidence.store import EvidenceStore
        from traceops.rca.ranking import RankedCandidate
        from traceops.rca.report import build_report

        from evaluation.investigation_profile import DEFAULT_PROFILE

        ts = datetime(2026, 10, 30, 10, 2, 0, tzinfo=UTC)
        hypothesis = RootCauseHypothesis(
            investigation_run_id="r", candidate_service="redis",
            fault_type="availability_degradation",
            failure_mechanism="m", predicted_observations=["error_logs"])
        log = Evidence(
            incident_id="i", investigation_run_id="r", evidence_type=EvidenceType.LOG,
            source_id="loki", service_id="redis", observed_at=ts,
            window_start=ts - timedelta(minutes=2), window_end=ts + timedelta(minutes=3),
            query_template_id="qt", query_params={},
            normalized_data={"kind": "log", "total_records": 1,
                             "records": [{"timestamp": ts.isoformat(),
                                          "message": "ERROR timeout", "labels": {}}]},
            checksum="")
        store = EvidenceStore()
        store.add(log)
        report = build_report(
            investigation_run_id="r", hypotheses=[hypothesis], links=[],
            evidence=[log], ranked=[RankedCandidate(
                hypothesis_id=hypothesis.id, score=3, rank=1,
                status=HypothesisStatus.SUPPORTED)],
            fault_start=ts, store=store, incident_id="i",
            profile_digest=DEFAULT_PROFILE.digest())

        assert report.profile_digest == DEFAULT_PROFILE.digest(), (
            "a report must record the strategy that produced it")

    def test_two_runs_with_different_profiles_are_distinguishable(self):
        from dataclasses import replace

        from evaluation.investigation_profile import DEFAULT_PROFILE

        tampered = replace(DEFAULT_PROFILE, error_rate_threshold=0.03)
        assert DEFAULT_PROFILE.digest() != tampered.digest()


class TestAblationAttributionIsNowMachineProvable:
    """消融的核心诉求：从"我声明只改一个变量"变成"机器证明只改一个变量"。"""

    def test_arms_sharing_a_profile_digest_are_provably_comparable(self):
        from dataclasses import replace

        from evaluation.investigation_profile import DEFAULT_PROFILE

        # 两个臂：只关掉一个闸门
        e4 = replace(DEFAULT_PROFILE, enable_semantic_gate=False)
        e5 = replace(DEFAULT_PROFILE, enable_semantic_gate=True)
        assert e4.digest() != e5.digest()
        # 差异必须**恰好**是那一个字段
        diff = {
            key for key in e4.to_dict()
            if key != "plan_templates" and e4.to_dict()[key] != e5.to_dict()[key]
        }
        assert diff == {"enable_semantic_gate"}, (
            f"arms must differ in exactly one field, got {diff}")

    def test_a_silent_threshold_change_breaks_attribution_visibly(self):
        """有人调了阈值 → digest 变化 → 归因失效**可被发现**。

        这是当前完全缺失的保护：没有 digest 时这种改动不可见，
        消融结论会在无声中失去意义。
        """
        from dataclasses import replace

        from evaluation.investigation_profile import DEFAULT_PROFILE

        e4 = replace(DEFAULT_PROFILE, enable_semantic_gate=False)
        tampered_e5 = replace(DEFAULT_PROFILE, enable_semantic_gate=True,
                              error_rate_threshold=0.04)
        # 正常情况下两臂只差闸门；被篡改后差异变多
        base_diff = {"enable_semantic_gate"}
        actual = {
            key for key in e4.to_dict()
            if key != "plan_templates" and e4.to_dict()[key] != tampered_e5.to_dict()[key]
        }
        assert actual != base_diff, "a tampered threshold must be detectable"


class TestAblationArmsUseRealProfiles:
    def test_every_arm_renders_a_profile(self):
        from evaluation.ablation import ARMS, arm_profile

        for arm_id in ARMS:
            profile = arm_profile(arm_id)
            assert profile.digest(), f"{arm_id} must produce a digestable profile"

    def test_arm_config_carries_the_profile_digest(self):
        """arm_config 必须带 digest —— 否则运行时无法证明策略一致。"""
        from evaluation.ablation import arm_config

        config = arm_config("E4")
        assert "profile_digest" in config, (
            "arm config must carry the strategy digest for verifiability")

    def test_e4_and_e5_profiles_differ_only_in_gates(self):
        """E4 与 E5 的 profile 差异必须恰好是两个闸门开关。"""
        from evaluation.ablation import arm_profile

        e4 = arm_profile("E4").to_dict()
        e5 = arm_profile("E5").to_dict()
        diff = {
            key for key in e4
            if key != "plan_templates" and e4[key] != e5[key]
        }
        assert diff == {"enable_semantic_gate", "enable_contradiction"}, (
            f"E4/E5 must differ only in verification gates, got {diff}")


class TestProfileDistinguishesEveryArm:
    """每个消融臂必须得到**不同**的 digest。

    ## 为什么这是真实缺陷（实跑发现）

    E3 与 E4 的 digest 完全相同 —— 因为 profile 里没有 `evidence_engine` 字段，
    而 E4 相对 E3 的唯一增量正是证据引擎。

    后果：`arm_config` 会给出相同的 `profile_digest`，
    于是"E3 → E4 只改了证据引擎"这个声明**无法被证明**，
    反而看起来像"这两个臂策略完全相同" —— 归因彻底失效。

    这正是引入 digest 想解决的问题本身：没有它，差异不可见；
    有它但**漏了字段**，差异同样不可见，只是看起来更可信。
    """

    def test_all_five_arms_have_distinct_digests(self):
        from evaluation.ablation import ARMS, arm_profile

        digests = {arm_id: arm_profile(arm_id).digest() for arm_id in ARMS}
        duplicates = {
            (a, b) for a in digests for b in digests
            if a < b and digests[a] == digests[b]
        }
        assert not duplicates, f"arms with identical strategy digests: {duplicates}"

    def test_evidence_engine_is_part_of_the_strategy(self):
        """证据引擎开关必须是策略的一部分 —— 否则 E3/E4 无法区分。"""
        from evaluation.investigation_profile import DEFAULT_PROFILE

        assert hasattr(DEFAULT_PROFILE, "enable_evidence_engine")

    def test_e3_and_e4_differ_only_in_evidence_engine(self):
        from evaluation.ablation import arm_profile

        e3 = arm_profile("E3").to_dict()
        e4 = arm_profile("E4").to_dict()
        diff = {k for k in e3 if k != "plan_templates" and e3[k] != e4[k]}
        assert diff == {"enable_evidence_engine"}, f"got {diff}"
