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

from traceops.prompts import prompt_version


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
    """可复现性快照（蓝图 §13.3）：两次跑同一事故结论不同，必须能查出差在哪。

    这里把**策略身份**和**本次运行输入**分成两部分（见
    `traceops/runtime/strategy.py`）：

    - `strategy_digest` / 各个 `*_version` 字段：判断标准是什么
    - `run_snapshot`：这次跑用了多少额度、看多深

    此前的写法把预算与深度拼进版本字符串
    （`workflow_version="p0-v1;max_tool_calls=50"`、
    `topology_version="file-declared;scope_depth=1"`），后果是
    **同样的策略、不同的预算看起来像换了策略**，而真正换了阈值或权重
    反而一处都没记。两个方向的错都犯过。
    """

    model_provider: str = "none"
    model_name: str = "deterministic-rules"
    workflow_version: str = "p0-v1"
    tool_schema_version: str = "p0-v1"
    evidence_schema_version: str = "1.0"
    topology_version: str = "file-declared"
    system_prompt_version: str | None = None
    dataset_version: str | None = None
    config_version: str | None = None
    #: 决策策略的内容指纹（阈值/权重/闸门/提示词）。
    strategy_digest: str | None = None
    #: 本次运行的实际输入（预算、采集深度、拓扑指纹、提示词版本）。
    run_snapshot: dict = field(default_factory=dict)

    @classmethod
    def from_runtime(cls, *, model_name: str | None, scope_depth: int,
                     max_tool_calls: int,
                     config_version: str | None = None,
                     strategy_digest: str | None = None,
                     run_snapshot: dict | None = None) -> "InvestigationConfigSnapshot":
        """从**实际运行配置**派生快照。

        为什么不能直接用默认值：默认值会让每次 run 的快照完全相同，
        于是「两次跑同一场景结论不同，查出差在哪」这个承诺**无法兑现**。
        更严重的是它会**撒谎** —— 接了模型却记成 `deterministic-rules`，
        事后复盘会得出错误结论。

        从运行时对象派生，而不是让调用方"记得填"：后者一定会漏。
        """
        provider = "none"
        name = "deterministic-rules"
        prompt_version_used: str | None = None
        if model_name:
            name = model_name
            # 客户端名形如 "openai-compatible:<model>"，前缀即 provider
            provider = model_name.split(":", 1)[0] if ":" in model_name else model_name
            # 提示词版本是**内容哈希**，不是手填的串：改一个字符版本就变，
            # 不改就一定不变。只有真的挂了模型，这次 run 才用到了提示词 ——
            # 纯规则跑的 run 记一个提示词版本反而是假信息。
            prompt_version_used = prompt_version()

        snapshot = dict(run_snapshot or {})
        # 采集深度与预算属于**运行输入**，不属于策略身份：它们决定这次
        # 看多远、跑多久，不决定"什么样的观测才算异常"。
        snapshot.setdefault("scope_depth", scope_depth)
        snapshot.setdefault("max_tool_calls", max_tool_calls)

        return cls(
            model_provider=provider,
            model_name=name,
            topology_version="file-declared",
            workflow_version="p0-v1",
            system_prompt_version=prompt_version_used,
            config_version=config_version,
            strategy_digest=strategy_digest,
            run_snapshot=snapshot,
        )

    def to_dict(self) -> dict[str, object]:
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
            "strategy_digest": self.strategy_digest,
            "run_snapshot": self.run_snapshot,
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
