"""Budget Manager (REVISIONS-1.1 R6)。

预算控制在 Agent Runtime（不是 Tool Gateway）: 只有发起 LLM 调用的地方
才能完整统计多轮对话的输入/输出 Token。

执行逻辑:
    reserve(estimate) → [模型调用] → settle(actual) / release(r)
    超预算的 reserve 抛 BudgetExhausted → 调查以当前已知结果收尾。

线程安全说明: P0 单 Worker 串行调查，不设锁；多 Worker 时在 Orchestrator 层
为每个 Run 分配独立 BudgetManager 实例。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

DEFAULT_MAX_TOKENS = 100_000  # 工程上限（REVISIONS-1.1 R6），不是平均预期
DEFAULT_MAX_TOOL_CALLS = 50
DEFAULT_MAX_ROUNDS = 10
#: 墙钟上限默认值。**必须是模块常量**：它同时被
#: `traceops.config.fields`（声明式旋钮的默认值来源）与文档引用，
#: 写在构造签名里就会出现"文档写 900、代码改了没人跟"的漂移。
DEFAULT_DEADLINE_SECONDS = 900


class BudgetExhausted(Exception):
    """预算耗尽/超限。调查流程捕获后走 Inconclusive 收尾，不重试。"""


@dataclass
class Reservation:
    id: str
    purpose: str
    estimate: int
    settled: bool = False
    actual: int | None = None


@dataclass
class _LedgerEntry:
    purpose: str
    estimate: int
    actual: int
    at_epoch: float = field(default_factory=time.time)


class BudgetManager:
    def __init__(
        self,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        # 墙钟上限，默认 15 分钟。
        #
        # 为什么必须有默认值：`max_tool_calls` 能拦住**次数**，拦不住
        # "50 次调用每次都很慢" —— 数据源持续慢响应时，一个调查可以无限期
        # 占住 worker。它还会连带破坏两件事：
        #   1. `min_idle_ms`（PEL 恢复门限）无法定标 —— 若调查可能跑 10 分钟，
        #      60 秒就会**误抢**正在正常处理的活，同一事故被两个 worker 同时查；
        #   2. 优雅停机被架空 —— SIGTERM 后要"处理完当前批次"，
        #      而没有上限的调查能让该批次永远不结束，最终仍被宽限期硬杀。
        #
        # 15 分钟的量级依据：纯确定性路径实测秒级完成；接入本地模型后
        # 单轮约 3 秒、7 节点约 20 秒。900 秒给远程模型与重试留足余量，
        # 同时远小于"永远"。需要更长时可显式传更大值，或传 None 关闭
        # （关闭是显式选择，不是默认）。
        deadline_seconds: float | None = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        self.max_tokens = max_tokens
        self.max_tool_calls = max_tool_calls
        self.max_rounds = max_rounds
        self.deadline_seconds = deadline_seconds
        self._started = time.monotonic()
        self._outstanding: dict[str, Reservation] = {}
        self._tokens_used = 0
        self._tool_calls = 0
        self._rounds = 0
        self._ledger: list[_LedgerEntry] = []

    # ---- token budget (reserve → call → settle/release) ----
    @property
    def tokens_used(self) -> int:
        return self._tokens_used

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def rounds(self) -> int:
        return self._rounds

    @property
    def tokens_remaining(self) -> int:
        outstanding = sum(r.estimate for r in self._outstanding.values())
        return max(0, self.max_tokens - self._tokens_used - outstanding)

    def reserve(self, *, estimate: int, purpose: str) -> Reservation:
        """调用模型前预留。余额不足直接拒绝——调用不会发生。"""
        if estimate > self.tokens_remaining:
            raise BudgetExhausted(
                f"token budget exhausted: need {estimate}, "
                f"remaining {self.tokens_remaining} "
                f"(used {self._tokens_used}, outstanding {sum(r.estimate for r in self._outstanding.values())})"
            )
        r = Reservation(id=uuid.uuid4().hex, purpose=purpose, estimate=estimate)
        self._outstanding[r.id] = r
        return r

    def settle(self, reservation: Reservation, *, actual: int) -> None:
        """模型调用完成后按实际用量记账。actual 可超过 estimate（调用已发生）。"""
        if reservation.settled:
            return
        self._outstanding.pop(reservation.id, None)
        reservation.settled = True
        reservation.actual = actual
        self._tokens_used += actual
        self._ledger.append(_LedgerEntry(purpose=reservation.purpose,
                                         estimate=reservation.estimate, actual=actual))

    def release(self, reservation: Reservation) -> None:
        """调用未执行（如模型客户端抛错），释放预留额度。"""
        self._outstanding.pop(reservation.id, None)
        reservation.settled = True  # mark terminal, no accounting

    # ---- tool call budget ----
    def consume_tool_call(self) -> None:
        self._tool_calls += 1
        if self._tool_calls > self.max_tool_calls:
            raise BudgetExhausted(f"tool call budget exhausted: limit {self.max_tool_calls}")

    @property
    def tool_calls_remaining(self) -> int:
        """还能发多少次工具调用。

        判别回路必须看这个数再决定要不要多问一趟 —— 否则回路会把
        预算花在一堆判别查询上，然后连出报告的余地都没有。
        """
        return max(0, self.max_tool_calls - self._tool_calls)

    # ---- round / deadline budget ----
    def begin_round(self) -> None:
        """开始**一轮取证**。

        「轮」的语义是"又跑了一趟数据源"，不是"又执行了一个节点"。
        此前每个图节点都算一轮，于是 7 节点图一开跑就吃掉 7 轮预算；
        加上判别回路（最多再取证两趟）后节点数会涨到 13，
        `max_rounds=10` 就会在回路真正需要预算时先被节点计数耗尽 ——
        上限还在，但限制的不是它想限制的东西。
        按取证趟数计轮，`max_rounds` 才等于"最多允许问几次数据源"。
        """
        self.check_deadline()
        self._rounds += 1
        if self._rounds > self.max_rounds:
            raise BudgetExhausted(f"round budget exhausted: limit {self.max_rounds}")

    def check_deadline(self) -> None:
        """只检查墙钟上限，不占轮次。

        给"不取证但也不该无限运行"的节点用（计划、推理、排序、出报告）：
        它们必须受 deadline 约束，但不该消耗取证轮次。
        """
        self._check_deadline()

    def _check_deadline(self) -> None:
        if self.deadline_seconds is not None and time.monotonic() - self._started > self.deadline_seconds:
            raise BudgetExhausted(f"deadline exceeded: {self.deadline_seconds}s")

    # ---- persistence ----
    def snapshot(self) -> dict:
        """用于 InvestigationRun 持久化的用量快照（blueprint §13.3）。"""
        return {
            "tokens_used": self._tokens_used,
            "tool_calls": self._tool_calls,
            "rounds": self._rounds,
            "limits": {
                "tokens": self.max_tokens,
                "tool_calls": self.max_tool_calls,
                "rounds": self.max_rounds,
            },
            "ledger": [
                {"purpose": e.purpose, "estimate": e.estimate, "actual": e.actual, "at": e.at_epoch}
                for e in self._ledger
            ],
        }
