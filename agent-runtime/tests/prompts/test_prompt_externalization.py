"""提示词外置：文件、版本、以及"版本号不能是手填的"。

## 锁定的四件事

1. 提示词**确实**在文件里、代码里只是加载 —— 否则"外置"是假的。
2. 版本是**内容哈希**，改一个字符就变。
3. `InvestigationConfigSnapshot.system_prompt_version` 不再是 `None`
   （挂了模型时），纯规则跑仍然是 `None`（记一个没被用过的提示词版本是假信息）。
4. `_SYSTEM_PROMPT` 与文件内容**逐字节一致** —— 两边能漂移的写法等于没外置。
5. 提示词由**命名片段**组成，且每一段能单独归因：改一条 few-shot
   不能顺带改掉 fault_type 边界定义的那一段版本（否则结论变差时说不清原因）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from traceops.prompts import (
    HYPOTHESIS_PROMPT,
    PROMPT_PARTS,
    Prompt,
    PromptPart,
    PromptNotFound,
    digest_of,
    load_part,
    load_prompt,
    part_order,
    prompt_part_versions,
    prompt_version,
)
from traceops.runtime.investigation_run import InvestigationConfigSnapshot

VERSION_RE = re.compile(r"^hypothesis-v2@[0-9a-f]{16}$")


class TestPromptLivesInAFile:
    def test_prompt_file_exists_and_is_not_empty(self):
        prompt = load_prompt()
        assert prompt.text, "提示词文件不能是空的"
        assert len(prompt.text) > 500, "提示词被截断了"

    def test_prompt_text_comes_from_the_files_not_the_code(self):
        """把每个片段文件读一遍，确认加载器只是按顺序拼接、没有改写内容。"""
        root = Path(__file__).resolve().parents[2] / "traceops" / "prompts" / "parts"
        texts = []
        for part in part_order():
            path = root / HYPOTHESIS_PROMPT / f"{part}.md"
            assert path.is_file(), f"片段文件不存在：{path}"
            texts.append(path.read_text(encoding="utf-8").strip())
        assert load_prompt().text == "\n\n".join(texts)

    def test_missing_prompt_lists_what_is_available(self):
        with pytest.raises(PromptNotFound) as excinfo:
            load_prompt("no-such-prompt")
        assert "hypothesis-v2" in str(excinfo.value), (
            "报错要说清有哪些提示词可用，否则只能去翻目录")


class TestVersionIsAContentHash:
    def test_version_shape(self):
        assert VERSION_RE.match(prompt_version()), prompt_version()

    def test_version_is_derived_from_the_part_versions(self):
        prompt = load_prompt()
        expected = hashlib.sha256(
            "\n".join(p.version for p in prompt.parts).encode("utf-8")).hexdigest()[:16]
        assert prompt.digest == expected
        assert len(prompt.parts) == len(PROMPT_PARTS[HYPOTHESIS_PROMPT])

    def test_one_character_change_changes_the_version(self):
        base = "You are the hypothesis generator."
        assert digest_of(base) != digest_of(base + " ")

    def test_same_text_same_version(self):
        assert digest_of("a b c") == digest_of("a b c")

    def test_version_includes_the_prompt_name(self):
        """只有哈希的话，换一份提示词但内容恰好相同也能"版本不变"；
        带上名字才能表达"是另一代提示词"。"""
        prompt = load_prompt(HYPOTHESIS_PROMPT)
        assert isinstance(prompt, Prompt)
        assert prompt.version.startswith(HYPOTHESIS_PROMPT + "@")


class TestPartsAreIndividuallyAttributable:
    def test_every_declared_part_is_loadable_and_non_empty(self):
        for name in PROMPT_PARTS[HYPOTHESIS_PROMPT]:
            part = load_part(HYPOTHESIS_PROMPT, name)
            assert part.text, f"片段 {name} 是空的"
            assert part.version == f"{name}@{part.digest}"

    def test_part_versions_are_listed_in_order(self):
        versions = prompt_part_versions()
        assert [v.split("@")[0] for v in versions] == list(part_order())

    def test_editing_one_part_changes_only_that_part_version(self):
        """这是拆片段的**唯一目的**：归因。

        改一条 few-shot 必须只改 `examples` 的版本。整段哈希时做不到 ——
        那正是"结论变差但说不清是边界定义变了还是提示词变长"的来源。
        """
        order = part_order()
        before = [load_part(HYPOTHESIS_PROMPT, name).version for name in order]

        edited = []
        for name in order:
            part = load_part(HYPOTHESIS_PROMPT, name)
            if name == "examples":
                text = part.text + "\n5. Clue: an extra example."
                part = PromptPart(name=name, text=text, digest=digest_of(text))
            edited.append(part)

        after = [part.version for part in edited]
        changed = [a for a, b in zip(after, before) if a != b]
        assert changed == [edited[order.index("examples")].version]
        # 组合版本也必须跟着变 —— 否则报告里的 `hypothesis-v2@...` 会说谎。
        assert _composed_digest(after) != _composed_digest(before)

    def test_unknown_part_name_is_reported_with_what_exists(self):
        with pytest.raises(PromptNotFound) as excinfo:
            load_part(HYPOTHESIS_PROMPT, "no-such-part")
        assert "role" in str(excinfo.value)


def _composed_digest(part_versions) -> str:
    """组合版本：片段版本列表的哈希（与 `load_prompt` 的算法一致）。"""
    return digest_of("\n".join(part_versions))


class TestSnapshotCarriesTheVersion:
    def test_model_run_records_the_real_prompt_version(self):
        snapshot = InvestigationConfigSnapshot.from_runtime(
            model_name="openai-compatible:qwen3:4b", scope_depth=2, max_tool_calls=40)

        assert snapshot.system_prompt_version == prompt_version()
        assert snapshot.to_dict()["system_prompt_version"] == prompt_version()

    def test_rules_only_run_does_not_claim_a_prompt_version(self):
        """没挂模型就没有提示词被使用。填一个版本会让复盘以为模型参与过。"""
        snapshot = InvestigationConfigSnapshot.from_runtime(
            model_name=None, scope_depth=1, max_tool_calls=50)

        assert snapshot.system_prompt_version is None
        assert snapshot.to_dict()["system_prompt_version"] is None


class TestCodeAndFileDoNotDrift:
    def test_module_constant_equals_file_content(self):
        from traceops.runtime.openai_client import _SYSTEM_PROMPT
        assert _SYSTEM_PROMPT == load_prompt().text, (
            "openai_client 里的提示词与 prompts/hypothesis-v2 的各片段不一致")

    def test_module_version_equals_file_version(self):
        from traceops.runtime.openai_client import SYSTEM_PROMPT_VERSION
        assert SYSTEM_PROMPT_VERSION == prompt_version()


class TestSnapshotCarriesPartVersions:
    """快照要能回答"这次提示词由哪几个片段组成、各自哪一版"。

    只记总哈希时，两次跑的 `hypothesis-v2@...` 不同，也仍然说不出是哪一段变的。
    """

    def test_run_snapshot_lists_every_part_version(self):
        from traceops.runtime.strategy import run_snapshot

        snapshot = run_snapshot(
            prompt_version=prompt_version(), prompt_parts=prompt_part_versions())

        assert snapshot["prompt_version"] == prompt_version()
        assert list(snapshot["prompt_parts"]) == list(prompt_part_versions())
        assert [entry.split("@")[0] for entry in snapshot["prompt_parts"]] == list(part_order())

    def test_a_rules_only_run_records_no_parts(self):
        """纯规则跑没有提示词参与，逐片段列表必须是空的而不是"当前版本"。"""
        from traceops.evidence.store import EvidenceStore
        from traceops.orchestration.graph import RunContext
        from traceops.runtime.budget import BudgetManager

        ctx = RunContext(store=EvidenceStore(), adapters=None,
                         budget=BudgetManager(max_tool_calls=1))
        assert ctx.prompt_part_versions() == ()
        assert ctx.run_snapshot()["prompt_parts"] == []
