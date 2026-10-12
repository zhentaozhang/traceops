"""Tool Gateway (blueprint §4.5)。

Agent 不直接调用 MCP Server：所有工具调用经过本网关执行
注册检查 → 权限检查 → 参数校验 → 限额 → 结果大小检查 → 审计。

边界（REVISIONS-1.1 R6）:
- 工具调用计数、查询超时、结果大小限制在这里
- Token 预算不在这里（在 Agent Runtime 的 BudgetManager / Model Gateway）
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
import time
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from traceops.runtime.budget import BudgetExhausted, BudgetManager


class ToolPermission(str, Enum):
    OBSERVABILITY_READ = "observability.read"
    REMEDIATION_WRITE = "remediation.write"


class ToolNotConfigured(RuntimeError):
    """工具已注册但数据源未配置。"""


class DuplicateToolCall(RuntimeError):
    """同一次调查里用完全相同的参数重复调用同一个工具。

    为什么要拒绝而不是放行：预算耗在重复查询上是真实成本
    （aurora-lab 记录过一个 run 把 2/3 预算花在 25 次重复写入上）。
    调用方应当换参数或换时间窗，而不是把同一件事再问一遍。
    """
    """内置工具尚未接数据源适配器（P0 阶段：先有契约，后接 Grafana MCP）。"""


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_schema: dict[str, Any]
    permission: ToolPermission
    timeout_seconds: float = 30.0
    max_result_bytes: int = 65536
    implementation: Callable[..., Any]
    audit_enabled: bool = True


#: 落盘结果在返回 dict 中的键。调用方据此知道"这不是全量"。
SPILL_REF_KEY = "_spill"
#: 预览字符数上限。固定值（不用上下文比例）：单次调用场景下上下文占用是固定的。
_SPILL_PREVIEW_CHARS = 2000
#: 单次读回的默认字符数。与 super-agent 的 `textPage` 同量级（4000 UTF-16 单位），
#: 目的是让"读完一份大结果"变成若干次可预算的分页，而不是一次塞满上下文。
_SPILL_PAGE_CHARS = 4000
#: 单页硬上限。超过这个值的请求一律被夹住 —— 分页的意义就是不让调用方一次要太多。
_SPILL_PAGE_MAX_CHARS = 8000


class SpillStore:
    """把超大工具结果落盘，**内容寻址**保存。

    为什么内容寻址：同一份数据重复落盘是同一个文件，
    因此落盘产物天然可去重、可复现，并且能直接当作证据引用
    （证据的 checksum 也是内容哈希，两者可以对齐）。

    为什么不用 LLM 摘要：holmesgpt 自己把摘要标成 LEGACY ——
    「summarization is lossy (the original tool output is unrecoverable afterwards)」。
    对证据引擎尤其致命：摘要后的内容无法内容寻址、无法引用校验、
    每次运行还不一样，直接破坏「证据可核查」这个立身之本。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def save(self, tool_name: str, payload: Any) -> dict[str, Any]:
        """写入并返回引用信息。写盘失败时抛 OSError，由调用方降级。"""
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        # 内容哈希：同内容 → 同文件（去重 + 可复现）
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        safe_tool = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tool_name)[:40]
        directory = self.root / safe_tool
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest[:16]}.json"
        path.write_text(encoded, encoding="utf-8")
        return {
            "path": str(path),
            "bytes": len(encoded.encode("utf-8")),
            "digest": digest,
            "preview": encoded[:_SPILL_PREVIEW_CHARS],
            "truncated": True,
        }

    # ---- 读回 ----
    #
    # 只有 save 的时候，落盘等于"把证据扔进了黑洞"：调用方拿到一个指针，
    # 却没有任何路径把它换回内容。对证据引擎来说这比丢弃更糟 ——
    # 审计说「结果已保留」，实际不可读。
    #
    # 读回必须同时守住三件事，缺一条就是漏洞：
    #   1. **路径围栏**：引用里带的是绝对路径，直接 open() 就能读任意文件。
    #      解析后必须仍在 root 之内。
    #   2. **内容校验**：落盘是内容寻址的，读回时重算摘要与引用里的 digest 比对。
    #      不一致说明文件被换过 —— 这条路径专门用来把"原始数据"喂给证据链，
    #      读回来的东西必须真的是当初写下去的那份。
    #   3. **分页**：一次读全量等于把溢出问题原样搬回上下文。

    def _confined_path(self, ref: dict[str, Any]) -> Path:
        raw_path = ref.get("path")
        if not raw_path:
            raise ValueError("spill reference has no path (spill_failed?)")
        resolved = Path(str(raw_path)).resolve()
        root = self.root.resolve()
        if not resolved.is_relative_to(root):
            # 明确的越界，不是"文件不存在"：后者可能只是被清理了，
            # 前者是有人构造了一个指向别处的引用。
            raise PermissionError(
                f"spill reference points outside the spill root: {resolved} not under {root}")
        return resolved

    def load(self, ref: dict[str, Any]) -> Any:
        """按引用取回完整内容（不截断）。路径越界或内容被改都抛异常。"""
        return json.loads(self._read_verified(ref))

    def page(self, ref: dict[str, Any], *, offset: int = 0,
             max_chars: int = _SPILL_PAGE_CHARS) -> dict[str, Any]:
        """分页读回一段文本（对应 super-agent 的 `textPage` 契约）。

        返回 `{text, offset, total_chars, next_offset, truncated}` ——
        `next_offset is None` 是"读完了"的唯一判据，调用方不必自己比较长度。
        """
        text = self._read_verified(ref)
        limit = max(1, min(int(max_chars), _SPILL_PAGE_MAX_CHARS))
        start = max(0, min(int(offset), len(text)))
        end = min(start + limit, len(text))
        return {
            "text": text[start:end],
            "offset": start,
            "total_chars": len(text),
            "next_offset": end if end < len(text) else None,
            "truncated": end < len(text),
        }

    def _read_verified(self, ref: dict[str, Any]) -> str:
        """围栏 + 摘要校验的统一读路径。

        分页也要校验：否则调用方可以把一个被换掉的文件分十页读完，
        每页都"成功"，拼起来却不是原件。
        """
        path = self._confined_path(ref)
        encoded = path.read_text(encoding="utf-8")
        expected = ref.get("digest")
        if expected:
            actual = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            if actual != expected:
                raise ValueError(
                    "spilled payload does not match its digest: "
                    f"expected {expected}, got {actual}")
        return encoded


class ToolGateway:
    def __init__(self, spill_root: Path | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._audit: list[dict[str, Any]] = []
        # 同一次调查内「同名 + 完全相同参数」的调用指纹（重复调用防护）
        self._seen_calls: set[str] = set()
        # 超大结果的落盘根目录。None 时用临时目录（不污染工作区）。
        self._spill_root = Path(spill_root) if spill_root is not None else (
            Path(tempfile.gettempdir()) / "traceops-spill")
        self._spill = SpillStore(self._spill_root)

    # ---- registry ----
    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get_tool(self, name: str) -> ToolSpec:
        return self._tools[name]

    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    # ---- invocation ----
    def invoke(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        budget: BudgetManager,
        allowed_permissions: list[ToolPermission] | None = None,
    ) -> Any:
        """执行一次受控工具调用。异常语义:
        - 未注册 / 权限不足 → PermissionError
        - 参数不合法 → jsonschema.ValidationError（原样抛出）
        - 预算耗尽 → BudgetExhausted
        """
        spec = self._tools.get(tool_name)
        if spec is None:
            self._record(tool_name, "UNKNOWN_TOOL", params, error="tool not registered")
            raise PermissionError(f"tool {tool_name!r} is not registered")

        allowed = allowed_permissions if allowed_permissions is not None else [ToolPermission.OBSERVABILITY_READ]
        if spec.permission not in allowed:
            self._record(tool_name, "DENIED_PERMISSION", params,
                         error=f"requires {spec.permission.value}")
            raise PermissionError(
                f"tool {tool_name!r} requires permission {spec.permission.value}; "
                f"allowed: {[p.value for p in allowed]}"
            )

        from jsonschema import ValidationError as _SchemaError

        try:
            Draft202012Validator(spec.input_schema).validate(params)
        except _SchemaError as exc:
            self._record(tool_name, "REJECTED_SCHEMA", params, error=exc.message[:500])
            raise

        # 重复调用防护（holmesgpt safeguards.prevent_overly_repeated_tool_call 的做法）：
        # 同一次调查里「同名 + 完全相同参数」的第二次调用直接拒绝。
        #
        # 必须放在预算扣除**之前**：否则"防重复"本身成了成本 ——
        # 一次重复调用会吃掉一次配额，反而加速预算耗尽。
        fingerprint = self._call_fingerprint(tool_name, params)
        if fingerprint in self._seen_calls:
            self._record(tool_name, "DUPLICATE_CALL", params,
                         error="identical call already made in this investigation")
            raise DuplicateToolCall(
                f"duplicate call: {tool_name} was already invoked with identical params; "
                "change the parameters or the time window instead"
            )

        try:
            budget.consume_tool_call()
        except BudgetExhausted as exc:
            self._record(tool_name, "BUDGET_EXHAUSTED", params, error=str(exc))
            raise

        self._seen_calls.add(fingerprint)
        started = time.monotonic()
        try:
            result = spec.implementation(**params)
        except Exception as exc:  # tool failure is audited, then propagated
            self._record(tool_name, "ERROR", params, error=repr(exc),
                         duration_ms=int((time.monotonic() - started) * 1000))
            raise

        encoded = json.dumps(result, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > spec.max_result_bytes:
            # **落盘而不是丢弃**。超过 max_result_bytes 是"数据量大"，
            # 不是"这次查询失败了" —— 对证据引擎来说，把正当查询的结果扔掉
            # 是最坏的失败模式（证据覆盖率下降、语义闸门降级，
            # 而根因只是数据量）。
            spilled = self._spill_or_truncate(tool_name, result, len(encoded))
            self._record(tool_name, "SPILLED", params,
                         duration_ms=int((time.monotonic() - started) * 1000))
            return spilled

        self._record(tool_name, self._outcome_for(result), params,
                     duration_ms=int((time.monotonic() - started) * 1000))
        return result

    @staticmethod
    def _call_fingerprint(tool_name: str, params: dict[str, Any]) -> str:
        """调用指纹：工具名 + 规范化 JSON 参数。

        `sort_keys=True` 是必须的：同一组参数换个键序不该被当成不同查询，
        否则指纹防护形同虚设。
        """
        return f"{tool_name}:{json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)}"

    @staticmethod
    def _is_empty_result(result: Any) -> bool:
        """判断这次查询是不是「查了，但什么都没有」。

        必须覆盖真实数据源的三种空响应形状（它们的共同点是"结果集为空"）：
          - Prometheus `{"status":"success","data":{"resultType":"matrix","result":[]}}`
          - Loki       `{"status":"success","data":{"resultType":"streams","result":[]}}`
          - Tempo      `{"traces_considered":0,"batches":[]}`

        **没有异常 ≠ 没有数据**：空字符串、空 list/dict 都算 no data。
        但 `0` 和 `False` 不算 —— 它们是合法的值（错误率 0 是真实的观测）。
        """
        if result is None:
            return True
        if isinstance(result, (str, list, dict, tuple, set)) and len(result) == 0:
            return True
        if isinstance(result, dict):
            for key in ("result", "records", "spans", "series", "batches", "traces"):
                value = result.get(key)
                if isinstance(value, (list, tuple)) and len(value) == 0:
                    return True
            data = result.get("data")
            if isinstance(data, dict) and isinstance(data.get("result"), list) and not data["result"]:
                return True
            if result.get("traces_considered") == 0:
                return True
        return False

    def _outcome_for(self, result: Any) -> str:
        """成功返回时区分 OK / NO_DATA。

        这个区别对**结论**有决定性意义：
          - NO_DATA = 查过了，确实没有 → 可以支持 REFUTED
          - ERROR   = 没法查           → 只能进 missing_data，禁止 REFUTED

        混为一谈会把"数据源挂了"当成"没有异常"，进而把 absence 当成阴性证据。
        """
        return "NO_DATA" if self._is_empty_result(result) else "OK"

    def _spill_or_truncate(self, tool_name: str, result: Any, size: int) -> dict[str, Any]:
        """落盘；写盘失败时降级为「带标记的截断」，**不让整个调用失败**。

        降级形态仍然明确标记 `spill_failed=True` 和 `truncated=True` ——
        调用方必须知道"你看到的不是全量、而且原件也不在".
        """
        try:
            return {SPILL_REF_KEY: self._spill.save(tool_name, result)}
        except OSError as exc:
            encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
            return {SPILL_REF_KEY: {
                "path": None,
                "bytes": size,
                "preview": encoded[:_SPILL_PREVIEW_CHARS],
                "truncated": True,
                "spill_failed": True,
                "error": str(exc)[:200],
            }}

    # ---- spill read-back ----
    @staticmethod
    def is_spill_reference(result: Any) -> bool:
        """这个返回值是"指针"还是"数据"。"""
        return isinstance(result, dict) and SPILL_REF_KEY in result

    def load_spilled(self, result: dict[str, Any]) -> Any:
        """把 spill 引用换回完整原始内容。

        给**编排层**用（证据归一化需要全量），不是给模型用 —— 模型侧走
        `page_spilled`，一次只给一页，避免把溢出问题原样搬回上下文。
        """
        ref = result[SPILL_REF_KEY]
        if ref.get("spill_failed"):
            raise ValueError(
                "spilled payload was never written: " + str(ref.get("error", "unknown")))
        return self._spill.load(ref)

    def page_spilled(self, result: dict[str, Any], *, offset: int = 0,
                     max_chars: int = _SPILL_PAGE_CHARS) -> dict[str, Any]:
        """分页读回（模型/报告侧）。"""
        ref = result[SPILL_REF_KEY]
        if ref.get("spill_failed"):
            raise ValueError(
                "spilled payload was never written: " + str(ref.get("error", "unknown")))
        return self._spill.page(ref, offset=offset, max_chars=max_chars)

    # ---- audit ----
    def _record(self, tool: str, outcome: str, params: dict[str, Any],
                *, error: str | None = None, duration_ms: int = 0) -> None:
        self._audit.append({
            "tool": tool,
            "outcome": outcome,
            "params": params,
            "error": error,
            "duration_ms": duration_ms,
            "timestamp": datetime.now(UTC),
        })

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)

    def audit_records(self, *, outcome: str) -> list[dict[str, Any]]:
        """按 outcome 过滤审计记录。

        有用是因为上层要回答两类不同的问题：
          - `outcome="NO_DATA"` → 哪些查询"查过但没东西"
          - `outcome="ERROR"`   → 哪些查询"根本没查成"
        """
        return [r for r in self._audit if r["outcome"] == outcome]


# ---- built-in tool pool (全部只读) ----

def _unconfigured(name: str) -> Callable[..., Any]:
    def _raise(**_params: Any) -> Any:
        raise ToolNotConfigured(
            f"tool {name!r} has no data-source adapter configured yet "
            f"(P0: wire Grafana MCP in agent-runtime/tools/adapters.py)"
        )
    return _raise


_TIME_RANGE_PROPS = {
    "start": {"type": "string", "format": "date-time"},
    "end": {"type": "string", "format": "date-time"},
}


def register_builtin_tools(gw: ToolGateway, adapters: Any | None = None) -> None:
    """注册 P0 内置工具。adapters 未提供时实现抛 ToolNotConfigured。"""
    tools = [
        ToolSpec(
            name="query_metric_series",
            description="查询指定服务在时间窗口内的 Prometheus 指标序列",
            input_schema={
                "type": "object",
                "required": ["service", "metric", "start", "end"],
                "properties": {
                    "service": {"type": "string"},
                    "metric": {"type": "string"},
                    **_TIME_RANGE_PROPS,
                },
                "additionalProperties": False,
            },
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=getattr(adapters, "query_metric_series", None) or _unconfigured("query_metric_series"),
        ),
        ToolSpec(
            name="query_service_error_rate",
            description="查询指定服务在时间窗口内的 HTTP 错误率",
            input_schema={
                "type": "object",
                "required": ["service", "start", "end"],
                "properties": {
                    "service": {"type": "string"},
                    **_TIME_RANGE_PROPS,
                },
                "additionalProperties": False,
            },
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=getattr(adapters, "query_service_error_rate", None) or _unconfigured("query_service_error_rate"),
        ),
        ToolSpec(
            name="query_service_logs",
            description="查询指定服务在时间窗口内匹配过滤条件的日志",
            input_schema={
                "type": "object",
                "required": ["service", "start", "end"],
                "properties": {
                    "service": {"type": "string"},
                    "filter": {"type": "string"},
                    **_TIME_RANGE_PROPS,
                },
                "additionalProperties": False,
            },
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=getattr(adapters, "query_service_logs", None) or _unconfigured("query_service_logs"),
        ),
        ToolSpec(
            name="query_service_traces",
            description="查询指定服务在时间窗口内的错误/慢 Trace 摘要",
            input_schema={
                "type": "object",
                "required": ["service", "start", "end"],
                "properties": {
                    "service": {"type": "string"},
                    "min_duration_ms": {"type": "integer", "minimum": 0},
                    **_TIME_RANGE_PROPS,
                },
                "additionalProperties": False,
            },
            permission=ToolPermission.OBSERVABILITY_READ,
            implementation=getattr(adapters, "query_service_traces", None) or _unconfigured("query_service_traces"),
        ),
    ]
    for tool in tools:
        gw.register(tool)
