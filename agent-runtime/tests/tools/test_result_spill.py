"""超大工具结果必须**落盘保留**，而不是抛异常丢弃。

## 缺陷（参考仓库分析确认，实读代码）

`gateway.invoke` 现在：

```python
if len(encoded) > spec.max_result_bytes:      # 默认 64KB
    self._record(..., "RESULT_TOO_LARGE", ...)
    raise ValueError(...)                      # 原始数据永久丢失
```

对**证据引擎**来说这是最坏的失败模式：一个完全正当的查询，因为结果大了一点，
就既拿不到证据、又拿不到原始数据。真实规模下 Prometheus matrix / Loki streams /
Tempo batches 轻松超 64KB —— 于是证据覆盖率下降、语义闸门降级，
而根因只是"数据量"。

## 正确做法（holmesgpt `tool_context_window_limiter.py:33` 的做法）

**落盘 + 指针 + 预览**，而不是丢弃：

- 原始内容写到一个可复现的位置
- 上下文里只放：`[spilled to <path>, N bytes, showing first M]` + 预览
- 模型/调用方知道"你看的是截断版"，并能按路径取回

为什么不用 LLM 摘要替代：holmesgpt 自己把摘要标成 LEGACY ——
「summarization is lossy (the original tool output is unrecoverable afterwards)」。
对 TraceOps 尤其致命：摘要后的内容**无法内容寻址、无法引用校验、每次跑还不一样**，
直接破坏「证据可核查」这个立身之本。

## 三条硬约束

1. **落盘失败要能降级**：不能因为写盘失败导致整个调用失败
2. **预览必须带明确标记**：否则模型会基于前 N 字节下结论
3. **不影响小结果**：64KB 以内的结果仍走原路径（零开销）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from traceops.runtime.budget import BudgetManager
from traceops.tools.gateway import (
    SPILL_REF_KEY, SpillStore, ToolGateway, ToolPermission, ToolSpec,
)


def _gateway(spill_root: Path | None = None) -> ToolGateway:
    gw = ToolGateway(spill_root=spill_root)
    gw.register(ToolSpec(
        name="big", description="returns a large payload",
        input_schema={"type": "object", "properties": {"n": {"type": "integer"}},
                      "required": ["n"], "additionalProperties": False},
        permission=ToolPermission.OBSERVABILITY_READ,
        max_result_bytes=1024,
        implementation=lambda n: {"rows": [{"i": i, "pad": "x" * 40} for i in range(n)]},
    ))
    gw.register(ToolSpec(
        name="small", description="returns something small",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        permission=ToolPermission.OBSERVABILITY_READ,
        implementation=lambda: {"ok": True},
    ))
    return gw


class TestOversizeResultIsSpilledNotDropped:
    def test_large_result_does_not_raise(self, tmp_path):
        gw = _gateway(tmp_path)
        # 不该抛异常 —— 数据量大不是错误
        result = gw.invoke("big", {"n": 200}, budget=BudgetManager())
        assert isinstance(result, dict)

    def test_large_result_returns_a_spill_reference(self, tmp_path):
        gw = _gateway(tmp_path)
        result = gw.invoke("big", {"n": 200}, budget=BudgetManager())
        assert SPILL_REF_KEY in result, f"no spill reference in {list(result)}"

    def test_spilled_file_contains_the_full_payload(self, tmp_path):
        """关键：原始数据必须完整保留，不是只留预览。"""
        gw = _gateway(tmp_path)
        result = gw.invoke("big", {"n": 200}, budget=BudgetManager())
        ref = result[SPILL_REF_KEY]
        path = Path(ref["path"])
        assert path.exists(), f"spill file missing: {path}"
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert len(stored["rows"]) == 200, (
            f"spill must keep the whole payload, got {len(stored['rows'])} rows")

    def test_result_carries_a_marked_preview(self, tmp_path):
        """预览必须明确标记为截断，否则模型会基于它下结论。"""
        gw = _gateway(tmp_path)
        result = gw.invoke("big", {"n": 200}, budget=BudgetManager())
        preview = result[SPILL_REF_KEY]["preview"]
        assert preview
        assert SPILL_REF_KEY in result
        assert result[SPILL_REF_KEY]["truncated"] is True

    def test_audit_records_the_spill_not_an_error(self, tmp_path):
        gw = _gateway(tmp_path)
        gw.invoke("big", {"n": 200}, budget=BudgetManager())
        outcomes = [r["outcome"] for r in gw.audit_log()]
        assert "SPILLED" in outcomes, outcomes
        assert "RESULT_TOO_LARGE" not in outcomes, (
            "spilling is the normal path now, not an error")


class TestSmallResultsAreUnchanged:
    def test_small_result_passes_through_untouched(self):
        gw = _gateway()
        result = gw.invoke("small", {}, budget=BudgetManager())
        assert result == {"ok": True}
        assert SPILL_REF_KEY not in result


class TestSpillFailureDegradesGracefully:
    def test_unwritable_spill_root_falls_back_to_marked_truncation(self):
        """写盘失败时不能整个调用失败 —— 降级为「带标记的截断」。"""
        gw = ToolGateway(spill_root=Path("/proc/nonexistent-dir"))
        gw.register(ToolSpec(
            name="big", description="big", input_schema={"type": "object",
                "properties": {"n": {"type": "integer"}}, "required": ["n"],
                "additionalProperties": False},
            permission=ToolPermission.OBSERVABILITY_READ,
            max_result_bytes=1024,
            implementation=lambda n: {"rows": [{"i": i} for i in range(n)]},
        ))
        result = gw.invoke("big", {"n": 200}, budget=BudgetManager())
        # 仍然成功返回，且明确标记"被截断、无法落盘"
        assert isinstance(result, dict)
        assert SPILL_REF_KEY in result
        assert result[SPILL_REF_KEY].get("spill_failed") is True


class TestSpillStore:
    def test_spill_is_content_addressed(self, tmp_path):
        """文件名基于内容哈希 —— 同一份数据落盘两次是同一个文件。

        这样落盘产物天然可去重、可复现，也能直接当证据引用。
        """
        store = SpillStore(tmp_path)
        payload = {"a": 1, "b": [1, 2, 3]}
        first = store.save("tool-a", payload)
        second = store.save("tool-a", payload)
        assert first["path"] == second["path"], "same content must map to same path"

    def test_different_content_different_path(self, tmp_path):
        store = SpillStore(tmp_path)
        a = store.save("t", {"x": 1})
        b = store.save("t", {"x": 2})
        assert a["path"] != b["path"]
