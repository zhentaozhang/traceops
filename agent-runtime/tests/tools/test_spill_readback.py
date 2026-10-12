"""落盘的结果必须**读得回来**。

## 缺陷

`SpillStore` 只有 `save`，没有 `load`。Gateway 把超大结果写到磁盘、
返回一个指针 `{_spill: {path, digest, preview, ...}}`，然后**全仓没有
任何代码读它**（实测：`read_spill` / `_spill.load` 零命中）。

对证据引擎来说这比丢弃更糟：审计记录说「结果已保留」（outcome=SPILLED），
实际上原件不可读。声称完成、实际不成立 —— 这正是这个仓库反复出现的病症。

而且它已经在制造真实的数据丢失：编排层把 `{_spill: ...}` 指针直接交给
Normalizer，三个归一化器全部拒绝，于是**一次完全成功的查询**被记进
`missing_data`。证据覆盖率下降的根因居然只是"数据量超过 64KB"。

## 三条硬约束

1. **路径围栏**：引用里带的是绝对路径。直接 `Path(p).read_text()` 等于
   给调用方一个任意文件读取原语。解析后必须仍在 spill root 之内。
2. **内容校验**：落盘是内容寻址的，读回时重算 sha256 与引用里的 digest
   比对。这条路径专门用来把"原始数据"喂给证据链 —— 读回来的必须真的是
   当初写下去的那份。
3. **分页**：一次读全量等于把溢出问题原样搬回上下文。分页的终止判据必须
   是 `next_offset is None`，不是"这一页比请求的短"。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceops.runtime.budget import BudgetManager
from traceops.tools.gateway import (
    SPILL_REF_KEY, SpillStore, ToolGateway, ToolPermission, ToolSpec,
)


def _gateway(spill_root: Path) -> ToolGateway:
    gw = ToolGateway(spill_root=spill_root)
    gw.register(ToolSpec(
        name="big", description="returns a large payload",
        input_schema={"type": "object", "properties": {"n": {"type": "integer"}},
                      "required": ["n"], "additionalProperties": False},
        permission=ToolPermission.OBSERVABILITY_READ,
        max_result_bytes=1024,
        implementation=lambda n: {"rows": [{"i": i, "pad": "x" * 40} for i in range(n)]},
    ))
    return gw


def _spill(gw: ToolGateway, n: int = 200) -> dict:
    return gw.invoke("big", {"n": n}, budget=BudgetManager())


class TestSpilledPayloadCanBeReadBack:
    def test_load_returns_the_whole_payload(self, tmp_path):
        gw = _gateway(tmp_path)
        result = _spill(gw)
        recovered = gw.load_spilled(result)
        assert len(recovered["rows"]) == 200, (
            f"read-back must return the whole payload, got {len(recovered['rows'])} rows")

    def test_stored_digest_matches_the_content(self, tmp_path):
        """落盘的内容哈希必须与引用里的一致 —— 否则引用不可校验。"""
        gw = _gateway(tmp_path)
        result = _spill(gw)
        ref = result[SPILL_REF_KEY]
        stored = json.loads(Path(ref["path"]).read_text(encoding="utf-8"))
        assert json.dumps(stored, ensure_ascii=False, sort_keys=True) == \
            json.dumps(gw.load_spilled(result), ensure_ascii=False, sort_keys=True)

    def test_is_spill_reference_distinguishes_pointer_from_data(self, tmp_path):
        gw = _gateway(tmp_path)
        assert gw.is_spill_reference(_spill(gw)) is True
        assert gw.is_spill_reference({"rows": []}) is False
        assert gw.is_spill_reference([1, 2, 3]) is False

    def test_unwritable_spill_reference_is_not_readable(self):
        """写盘失败时引用里没有 path，读回必须明确失败，不能返回空。"""
        gw = ToolGateway(spill_root=Path("/proc/nonexistent-dir"))
        gw.register(ToolSpec(
            name="big", description="big",
            input_schema={"type": "object", "properties": {"n": {"type": "integer"}},
                          "required": ["n"], "additionalProperties": False},
            permission=ToolPermission.OBSERVABILITY_READ,
            max_result_bytes=1024,
            implementation=lambda n: {"rows": [{"i": i} for i in range(n)]},
        ))
        result = gw.invoke("big", {"n": 200}, budget=BudgetManager())
        assert gw.is_spill_reference(result)
        with pytest.raises(ValueError, match="never written"):
            gw.load_spilled(result)


class TestPathFence:
    def test_reference_outside_the_root_is_refused(self, tmp_path):
        """引用可以被构造 —— 指向别处时必须拒绝，而不是照读。"""
        root = tmp_path / "spill"
        root.mkdir()
        outside = tmp_path / "secret.json"
        outside.write_text('{"secret": "leaked"}', encoding="utf-8")

        store = SpillStore(root)
        forged = {"path": str(outside), "digest": None, "truncated": True}
        with pytest.raises(PermissionError, match="outside the spill root"):
            store.load(forged)

    def test_parent_escape_is_refused(self, tmp_path):
        root = tmp_path / "spill"
        (root / "tool").mkdir(parents=True)
        store = SpillStore(root)
        forged = {"path": str(root / "tool" / ".." / ".." / "secret.json"), "digest": None}
        with pytest.raises(PermissionError):
            store.load(forged)

    def test_reference_without_a_path_is_refused(self, tmp_path):
        store = SpillStore(tmp_path)
        with pytest.raises(ValueError, match="no path"):
            store.load({"spill_failed": True, "truncated": True})


class TestDigestVerification:
    def test_tampered_file_is_refused(self, tmp_path):
        """文件被换掉时不能"读成功" —— 这条路径喂给的是证据链。"""
        store = SpillStore(tmp_path)
        ref = store.save("tool", {"a": 1})
        Path(ref["path"]).write_text('{"a": 2}', encoding="utf-8")
        with pytest.raises(ValueError, match="does not match its digest"):
            store.load(ref)

    def test_paging_also_verifies_the_digest(self, tmp_path):
        """否则可以把一个被换掉的文件分十页读完，每页都"成功"。"""
        store = SpillStore(tmp_path)
        ref = store.save("tool", {"a": "x" * 100})
        Path(ref["path"]).write_text('{"a": "y"}', encoding="utf-8")
        with pytest.raises(ValueError, match="does not match its digest"):
            store.page(ref)


class TestPaging:
    def test_first_page_reports_a_next_offset(self, tmp_path):
        store = SpillStore(tmp_path)
        ref = store.save("tool", {"text": "x" * 5000})
        page = store.page(ref, max_chars=1000)
        assert page["offset"] == 0
        assert len(page["text"]) == 1000
        assert page["total_chars"] > 1000
        assert page["next_offset"] == 1000
        assert page["truncated"] is True

    def test_last_page_terminates_with_none(self, tmp_path):
        """终止判据是 `next_offset is None`，不是"这页比较短"。"""
        store = SpillStore(tmp_path)
        ref = store.save("tool", {"text": "abc"})
        page = store.page(ref, max_chars=8000)
        assert page["next_offset"] is None
        assert page["truncated"] is False

    def test_walking_all_pages_reconstructs_the_file(self, tmp_path):
        """分页拼起来必须等于原件，一位不差。"""
        store = SpillStore(tmp_path)
        ref = store.save("tool", {"rows": list(range(500))})
        original = Path(ref["path"]).read_text(encoding="utf-8")

        chunks: list[str] = []
        offset = 0
        for _ in range(100):  # 防御：分页不该无限循环
            page = store.page(ref, offset=offset, max_chars=997)
            chunks.append(page["text"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        else:  # pragma: no cover
            pytest.fail("paging never terminated")
        assert "".join(chunks) == original

    def test_page_size_is_clamped(self, tmp_path):
        """单页硬上限：分页的意义就是不让调用方一次要太多。"""
        store = SpillStore(tmp_path)
        ref = store.save("tool", {"text": "x" * 20000})
        page = store.page(ref, max_chars=10_000_000)
        assert len(page["text"]) <= 8000

    def test_offset_past_the_end_returns_empty_without_error(self, tmp_path):
        store = SpillStore(tmp_path)
        ref = store.save("tool", {"text": "abc"})
        page = store.page(ref, offset=9999)
        assert page["text"] == ""
        assert page["next_offset"] is None
