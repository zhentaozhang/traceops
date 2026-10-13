"""提示词外置：提示词是行为的一部分，必须能版本化、能 diff、能回滚。

## 为什么必须搬出代码

提示词原先硬编码在 `runtime/openai_client.py` 的 `_SYSTEM_PROMPT` 里。
三个后果都是实际发生过的：

1. **改提示词等于改代码**：调整一句措辞会出现在代码 diff 里，和逻辑改动
   混在一起；想对比"v1 与 v2 哪个更准"，只能靠记住 commit hash。
2. **版本字段是假的**：`InvestigationConfigSnapshot.system_prompt_version`
   一直存在、一直是 `None`，`to_dict()` 忠实地把它序列化成 `null`。
   于是「两次跑同一事故结论不同，查出差在哪」这个承诺在提示词这一项上
   **恰好无法兑现** —— 而提示词恰恰是最常被改的那一项。
3. **无法回答"这次用的是哪版"**：事后复盘只能看到 `model_name`。

## 版本怎么算

内容哈希，不是手填的字符串。手填的版本号会被忘记递增，内容哈希不会：
改一个字符版本就变，不改就一定不变。前缀取 16 位十六进制，够用且可读。

`version` 形如 `hypothesis-v1@3f2a9c1b4d5e6f70`。

## 为什么不引入模板引擎

提示词里没有需要运行时替换的变量 —— 所有可变输入（证据摘要、候选服务、
告警摘要）都在 user 消息里作为 JSON 传，这是有意的：系统提示词保持
**完全静态**，才能被哈希、被缓存、被逐字节比对。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_PROMPT_ROOT = Path(__file__).resolve().parent

#: 当前假设生成使用的提示词名。改名即视为新的一代提示词。
HYPOTHESIS_PROMPT = "hypothesis-v1"


@dataclass(frozen=True)
class Prompt:
    """一份已加载的提示词。"""

    name: str
    text: str
    digest: str

    @property
    def version(self) -> str:
        """可写进快照的版本串：`<name>@<digest16>`。"""
        return f"{self.name}@{self.digest}"


class PromptNotFound(FileNotFoundError):
    """提示词文件缺失。"""


def digest_of(text: str) -> str:
    """提示词内容的短哈希（16 位十六进制）。

    单独抽出来是为了可测：可以在不落盘的前提下验证"改一个字符版本就变"。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=None)
def load_prompt(name: str = HYPOTHESIS_PROMPT) -> Prompt:
    """按名加载提示词。

    缓存是有意的：一个 Run 内会多次构造模型请求，重复读盘没有意义；
    而提示词文件在进程生命周期内不会变（改了就重启，版本号随之变化）。
    """
    path = _PROMPT_ROOT / f"{name}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptNotFound(
            f"prompt file is missing: {path} (available: "
            + ", ".join(sorted(p.stem for p in _PROMPT_ROOT.glob("*.md")))
            + ")"
        ) from exc
    text = raw.strip()
    if not text:
        raise PromptNotFound(f"prompt file is empty: {path}")
    digest = digest_of(text)
    return Prompt(name=name, text=text, digest=digest)


def prompt_version(name: str = HYPOTHESIS_PROMPT) -> str:
    """只取版本串。快照字段用这个。"""
    return load_prompt(name).version
