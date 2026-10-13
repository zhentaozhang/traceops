"""提示词外置：文件、版本、以及"版本号不能是手填的"。

## 锁定的四件事

1. 提示词**确实**在文件里、代码里只是加载 —— 否则"外置"是假的。
2. 版本是**内容哈希**，改一个字符就变。
3. `InvestigationConfigSnapshot.system_prompt_version` 不再是 `None`
   （挂了模型时），纯规则跑仍然是 `None`（记一个没被用过的提示词版本是假信息）。
4. `_SYSTEM_PROMPT` 与文件内容**逐字节一致** —— 两边能漂移的写法等于没外置。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from traceops.prompts import (
    HYPOTHESIS_PROMPT,
    Prompt,
    PromptNotFound,
    digest_of,
    load_prompt,
    prompt_version,
)
from traceops.runtime.investigation_run import InvestigationConfigSnapshot

VERSION_RE = re.compile(r"^hypothesis-v1@[0-9a-f]{16}$")


class TestPromptLivesInAFile:
    def test_prompt_file_exists_and_is_not_empty(self):
        prompt = load_prompt()
        assert prompt.text, "提示词文件不能是空的"
        assert len(prompt.text) > 500, "提示词被截断了"

    def test_prompt_text_comes_from_the_file_not_the_code(self):
        """直接把文件读一遍，确认加载器没有偷偷改写内容。"""
        path = Path(__file__).resolve().parents[2] / "traceops" / "prompts" / "hypothesis-v1.md"
        assert path.is_file(), f"提示词文件不存在：{path}"
        assert load_prompt().text == path.read_text(encoding="utf-8").strip()

    def test_missing_prompt_lists_what_is_available(self):
        with pytest.raises(PromptNotFound) as excinfo:
            load_prompt("no-such-prompt")
        assert "hypothesis-v1" in str(excinfo.value), (
            "报错要说清有哪些提示词可用，否则只能去翻目录")


class TestVersionIsAContentHash:
    def test_version_shape(self):
        assert VERSION_RE.match(prompt_version()), prompt_version()

    def test_version_matches_file_content(self):
        prompt = load_prompt()
        expected = hashlib.sha256(prompt.text.encode("utf-8")).hexdigest()[:16]
        assert prompt.digest == expected

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
            "openai_client 里的提示词与 prompts/hypothesis-v1.md 不一致")

    def test_module_version_equals_file_version(self):
        from traceops.runtime.openai_client import SYSTEM_PROMPT_VERSION
        assert SYSTEM_PROMPT_VERSION == prompt_version()
