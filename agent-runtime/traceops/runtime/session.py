"""调查会话记录：把"这次调查怎么走的"落成一行行可回放的事实。

为什么需要它
------------
报告只写**结论**（诊断了什么、引用了哪几条证据、缺了什么）。它回答不了三个
运维最常问的问题：

* 这次为什么停了？是预算用完了、还是所有候选都被推翻了？
* 那 9 次查询分别查了什么、返回多大、有没有降级？
* 同一个事故重跑一次，为什么走了不同的路径？

现在这些只能靠翻进程日志 —— 而日志里没有 budget 余额、没有节点顺序，
一旦进程重启就没了。AIOpsLab 的 Session trace 就是为解决这个：
每次运行落一份结构化时间线。

三条刻意的设计
--------------
* **只记事实，不记证据原文**。载荷可能几百 KB，抄进 session 就等于把 spill
  机制白做了。这里记工具名、参数摘要（大小与关键字段）、返回字节数、是否命中缓存。
* **有上限**。一条调查最多 `MAX_EVENTS` 行，超出后只记一条 `session.truncated`。
  没有上限的日志在事故现场会变成第二场事故（磁盘写满）。
* **不配就没有**。`path=None` 时整个 recorder 是 no-op：默认行为一个字节都不变，
  想要会话记录的人显式打开（`--session-log-dir` / `TRACEOPS_SESSION_LOG_DIR`）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 单次调查最多写多少行。超出后的内容不再记 —— 一条卡死的调查不该把磁盘写满。
MAX_EVENTS = 5_000

#: 参数摘要里允许保留原值的字段（其余字段只记"存在 + 类型"）。
#: 名字类参数是调查的关键上下文；自由文本可能是几百 KB 的过滤表达式。
_SAFE_PARAM_KEYS = frozenset({"service", "metric", "start", "end", "min_duration_ms"})

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _digest_params(params: Any) -> dict[str, Any]:
    """参数摘要：安全的键留原值，其余只留"有多长"。"""
    if not isinstance(params, dict):
        return {"kind": type(params).__name__}
    summary: dict[str, Any] = {}
    for key, value in params.items():
        if key in _SAFE_PARAM_KEYS and isinstance(value, (str, int, float, bool)):
            summary[key] = value
        else:
            summary[key] = f"<{type(value).__name__} len={len(str(value))}>"
    return summary


def safe_name(value: str) -> str:
    """run id 会进文件名：不允许 `../` 之类的东西拼出别的路径。"""
    cleaned = _UNSAFE_FILENAME.sub("-", value).strip("-")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"cannot derive a file name from {value!r}")
    return cleaned


@dataclass
class SessionRecorder:
    """一次调查的结构化时间线。`path=None` 时所有方法都是 no-op。"""

    path: Path | None
    run_id: str = ""
    _handle: Any = None
    _seq: int = 0
    _written: int = 0
    _truncated: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.path is not None

    @property
    def written(self) -> int:
        return self._written

    # -- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> "SessionRecorder":
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    # -- 写入 -------------------------------------------------------------
    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        """记一条事件。永不抛异常 —— 记录失败不该让调查失败。"""
        self._seq += 1
        event = {"seq": self._seq, "at": _now().isoformat(), "kind": kind}
        if self.run_id:
            event["run_id"] = self.run_id
        for key, value in fields.items():
            event[key] = value if _jsonable(value) else str(value)
        self.events.append(event)
        if not self.enabled:
            return event
        if self._written >= MAX_EVENTS:
            if not self._truncated:
                self._truncated = True
                self._emit({"seq": self._seq, "at": event["at"], "kind": "session.truncated",
                            "reason": f"{MAX_EVENTS} events reached; the rest is not recorded"})
            return event
        self._emit(event)
        self._written += 1
        return event

    def _emit(self, event: dict[str, Any]) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            self._handle.flush()
        except OSError:
            # 磁盘满 / 文件被删：调查照常继续，报告里仍会写 session_ref，
            # 但文件是缺的 —— 这比"因为写日志失败而整条调查崩掉"更合理。
            self._handle = None

    # -- 便捷方法 ---------------------------------------------------------
    def tool_call(self, *, tool: str, params: Any, ok: bool, payload: Any = None,
                  error: str | None = None, cache_hit: bool = False) -> dict[str, Any]:
        return self.record(
            "tool_call",
            tool=tool,
            params=_digest_params(params),
            ok=ok,
            cache_hit=cache_hit,
            payload_bytes=_size_of(payload),
            error=error,
        )

    def node(self, name: str, **fields: Any) -> dict[str, Any]:
        return self.record("node", node=name, **fields)


def _size_of(payload: Any) -> int | None:
    if payload is None:
        return None
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _jsonable(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None), dict, list))


def recorder_for(run_id: str, *, directory: str | Path | None) -> SessionRecorder:
    """按目录 + run id 造一个 recorder。目录为空时返回被禁用的 recorder。"""
    if not directory:
        return SessionRecorder(path=None, run_id=run_id)
    return SessionRecorder(path=Path(directory) / f"{safe_name(run_id)}.jsonl", run_id=run_id)


def read_session(path: str | Path) -> list[dict[str, Any]]:
    """把落盘的会话读回来。给测试与运维排查用。"""
    events: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events
