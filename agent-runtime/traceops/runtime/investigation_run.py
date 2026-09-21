"""InvestigationRun 状态机（蓝图 §2.2 / §8.4）。

与 Incident 状态机是两套生命周期：Incident 是业务事故，Run 是一次 Agent 调查。
同一个 Incident 可以有多轮 Run（换模型、换策略、人工补证据），彼此不覆盖。

关键语义：**WAITING_FOR_APPROVAL 是合法挂起态**，不是失败也不是完成。
把它当成终态会导致"等审批的调查永远无法恢复"，这是最常见的实现错误。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class RunStatus(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)

    @property
    def suspended(self) -> bool:
        return self is RunStatus.WAITING_FOR_APPROVAL


# 白名单：新增状态必须显式声明可达边，否则默认不可达
_ALLOWED: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.QUEUED, RunStatus.CANCELLED},
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_FOR_APPROVAL,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.WAITING_FOR_APPROVAL: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
}


class IllegalRunTransition(Exception):
    def __init__(self, run_id: str, source: RunStatus, target: RunStatus) -> None:
        super().__init__(f"illegal run transition for {run_id}: {source.value} -> {target.value}")
        self.run_id = run_id
        self.source = source
        self.target = target


@dataclass
class InvestigationConfigSnapshot:
    """可复现性快照（蓝图 §13.3）：两次跑同一事故结论不同，必须能查出差在哪。"""

    model_provider: str = "none"
    model_name: str = "deterministic-rules"
    workflow_version: str = "p0-v1"
    tool_schema_version: str = "p0-v1"
    evidence_schema_version: str = "1.0"
    topology_version: str = "file-declared"
    system_prompt_version: str | None = None
    dataset_version: str | None = None
    config_version: str | None = None

    @classmethod
    def from_runtime(cls, *, model_name: str | None, scope_depth: int,
                     max_tool_calls: int,
                     config_version: str | None = None) -> "InvestigationConfigSnapshot":
        """从**实际运行配置**派生快照。

        为什么不能直接用默认值：默认值会让每次 run 的快照完全相同，
        于是「两次跑同一事故结论不同，查出差在哪」这个承诺**无法兑现**。
        更严重的是它会**撒谎** —— 接了模型却记成 `deterministic-rules`，
        事后复盘会得出错误结论。

        从运行时对象派生，而不是让调用方"记得填"：后者一定会漏。
        """
        provider = "none"
        name = "deterministic-rules"
        if model_name:
            name = model_name
            # 客户端名形如 "openai-compatible:<model>"，前缀即 provider
            provider = model_name.split(":", 1)[0] if ":" in model_name else model_name
        return cls(
            model_provider=provider,
            model_name=name,
            # 采集深度与预算都会改变结论，因此属于可复现性信息
            topology_version=f"file-declared;scope_depth={scope_depth}",
            workflow_version=f"p0-v1;max_tool_calls={max_tool_calls}",
            config_version=config_version,
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "workflow_version": self.workflow_version,
            "tool_schema_version": self.tool_schema_version,
            "evidence_schema_version": self.evidence_schema_version,
            "topology_version": self.topology_version,
            "system_prompt_version": self.system_prompt_version,
            "dataset_version": self.dataset_version,
            "config_version": self.config_version,
        }


@dataclass
class InvestigationRun:
    run_id: str
    incident_id: str
    status: RunStatus = RunStatus.CREATED
    aggregate_version: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    current_node: str | None = None
    checkpoint_id: str | None = None
    attempts: int = 0
    rounds: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    last_error: str | None = None
    config: InvestigationConfigSnapshot = field(default_factory=InvestigationConfigSnapshot)

    @classmethod
    def open(cls, *, incident_id: str, run_id: str | None = None,
             config: InvestigationConfigSnapshot | None = None) -> InvestigationRun:
        return cls(
            run_id=run_id or f"run-{uuid.uuid4()}",
            incident_id=incident_id,
            config=config or InvestigationConfigSnapshot(),
        )

    # ---- transitions ----
    def _to(self, target: RunStatus) -> None:
        if target not in _ALLOWED[self.status]:
            raise IllegalRunTransition(self.run_id, self.status, target)
        self.status = target
        self.aggregate_version += 1
        self.updated_at = datetime.now(UTC)

    def queue(self) -> None:
        self._to(RunStatus.QUEUED)

    def start(self, *, node: str | None = None) -> None:
        self._to(RunStatus.RUNNING)
        self.attempts += 1
        self.current_node = node

    def suspend_for_approval(self, *, node: str | None = None) -> None:
        """合法的挂起：必须能回到 RUNNING。"""
        self._to(RunStatus.WAITING_FOR_APPROVAL)
        self.current_node = node

    def resume(self, *, node: str | None = None) -> None:
        if self.status is not RunStatus.WAITING_FOR_APPROVAL:
            raise IllegalRunTransition(self.run_id, self.status, RunStatus.RUNNING)
        self._to(RunStatus.RUNNING)
        self.current_node = node

    def complete(self, *, report_ref: str | None = None) -> None:
        self._to(RunStatus.COMPLETED)
        self.current_node = None
        if report_ref is not None:
            self.last_error = None

    def fail(self, *, reason: str, node: str | None = None, checkpoint_id: str | None = None) -> None:
        """运行失败。checkpoint 保留，便于恢复而不是从头再来（蓝图 §8.4）。"""
        self._to(RunStatus.FAILED)
        self.last_error = reason
        self.current_node = node
        if checkpoint_id is not None:
            self.checkpoint_id = checkpoint_id

    def cancel(self) -> None:
        self._to(RunStatus.CANCELLED)

    # ---- bookkeeping ----
    def record_checkpoint(self, checkpoint_id: str) -> None:
        self.checkpoint_id = checkpoint_id

    def record_usage(self, *, tool_calls: int = 0, tokens: int = 0, rounds: int = 0) -> None:
        """用量只增不减：预算是硬上限，回填时不允许被调小。"""
        self.tool_calls = max(self.tool_calls, tool_calls)
        self.tokens_used = max(self.tokens_used, tokens)
        self.rounds = max(self.rounds, rounds)

    def snapshot(self) -> dict:
        """用于持久化/事件 payload。"""
        return {
            "run_id": self.run_id,
            "incident_id": self.incident_id,
            "status": self.status.value,
            "aggregate_version": self.aggregate_version,
            "current_node": self.current_node,
            "checkpoint_id": self.checkpoint_id,
            "attempts": self.attempts,
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "tokens_used": self.tokens_used,
            "last_error": self.last_error,
            "suspended": self.status.suspended,
            "terminal": self.status.terminal,
            "config": self.config.to_dict(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
