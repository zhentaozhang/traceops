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

## 为什么拆成命名片段

整段提示词做哈希只回答"变没变"，不回答"哪一变"。而提示词里混着三种
**生命周期完全不同**的东西：

- `role` / `boundaries`：定义"什么算 latency_degradation"，改它等于改判定语义，
  必须和评测一起改；
- `examples`：few-shot，调整它通常是为了修个例，不该让判定语义的版本一起变；
- `abstention` / `output`：输出契约，改它影响的是解析而不是推理。

混成一个哈希时，改一条 few-shot 会让"fault_type 边界定义的版本"也变，
于是 EVALUATION 的归因就断了：看到结论变差，说不清是边界定义变了还是
提示词变长了。所以每个片段单独哈希、单独出现在版本串里。

片段顺序由 `PROMPT_PARTS` 固定 —— 顺序是提示词语义的一部分。

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
HYPOTHESIS_PROMPT = "hypothesis-v2"

#: 提示词由哪些命名片段按什么顺序拼成。
#:
#: 一份声明同时服务三件事：加载（拼哪些文件）、版本（哪些片段进了哈希）、
#: 归因（改了哪一段）。散在代码里拼字符串做不到第三件。
PROMPT_PARTS: dict[str, tuple[str, ...]] = {
    "hypothesis-v2": ("role", "boundaries", "examples", "abstention", "output"),
}

_PARTS_ROOT = _PROMPT_ROOT / "parts"


@dataclass(frozen=True)
class PromptPart:
    """提示词里的一个命名片段。"""

    name: str
    text: str
    digest: str

    @property
    def version(self) -> str:
        return f"{self.name}@{self.digest}"


@dataclass(frozen=True)
class Prompt:
    """一份已加载的提示词（由有序片段拼成）。"""

    name: str
    text: str
    digest: str
    parts: tuple[PromptPart, ...] = ()

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
    parts = tuple(load_part(name, part) for part in part_order(name))
    text = "\n\n".join(part.text for part in parts)
    # 哈希的是**片段的版本列表**，不是拼好的文本：这样"增删片段"也会改版本，
    # 而只改某一段时版本变化能直接对应到那一段。
    digest = digest_of("\n".join(p.version for p in parts))
    return Prompt(name=name, text=text, digest=digest, parts=parts)


def part_order(name: str = HYPOTHESIS_PROMPT) -> tuple[str, ...]:
    """这份提示词由哪些片段按什么顺序组成。未知名字直接报错。"""
    try:
        return PROMPT_PARTS[name]
    except KeyError as exc:
        raise PromptNotFound(
            f"prompt {name!r} has no declared parts (known: "
            + ", ".join(sorted(PROMPT_PARTS))
            + ")"
        ) from exc


@lru_cache(maxsize=None)
def load_part(name: str, part: str) -> PromptPart:
    """加载单个片段。片段路径是 `parts/<prompt>/<part>.md`。"""
    path = _PARTS_ROOT / name / f"{part}.md"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        available = sorted(p.stem for p in (_PARTS_ROOT / name).glob("*.md"))
        raise PromptNotFound(
            f"prompt part is missing: {path} (available: "
            + (", ".join(available) or "none")
            + ")"
        ) from exc
    text = raw.strip()
    if not text:
        raise PromptNotFound(f"prompt part is empty: {path}")
    return PromptPart(name=part, text=text, digest=digest_of(text))


def prompt_part_versions(name: str = HYPOTHESIS_PROMPT) -> tuple[str, ...]:
    """每个片段的版本串，顺序即拼接顺序。

    报告/快照里带上它，"这次的提示词是哪几个片段、各自哪一版"就能逐条回答；
    只给一个总哈希时，看到结论变化最多只能说"提示词变了"。
    """
    return tuple(load_part(name, part).version for part in part_order(name))


def prompt_version(name: str = HYPOTHESIS_PROMPT) -> str:
    """只取版本串。快照字段用这个。"""
    return load_prompt(name).version
