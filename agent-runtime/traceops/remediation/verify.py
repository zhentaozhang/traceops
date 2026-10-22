"""执行后的恢复验证：三层结论，不是布尔。

    RECOVERED  观测值已经达标
    UNCHANGED  达标没做到（或压根测不了）
    WORSE      比执行前更糟

## 为什么不能返回布尔

`execute_plan` 现在的契约是 `verifier.verify(plan) -> bool`，于是"没验成"
和"验不成"在类型上无法区分：数据源连不上、条件是人看的自由文本、
阈值没达标 —— 三种情况的处置完全不同（前者该重试取证、中者该找人、
后者该回滚），却都被压成一个 False。这里给出三层结论 + 每条条件的具体读数，
`RecoveryVerifier` 再把 RECOVERED 折成 bool 交给既有执行器。

## 为什么"测不了"不算通过

这是本仓库反复出现的病症：默认值恰好是"没问题"。没有可测条件的计划
（`post_conditions=[]` 或全是自由文本）在这里得到一个明确的 UNCHANGED，
理由写清"无法自动验证" —— 它会让 `execute_plan` 返回 NOT_VERIFIED 而不是
SUCCESS。宁可让一次没有后置条件的执行看起来"没验成"，也不要让它看起来"好了"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from traceops.remediation.plan import RemediationPlan
from traceops.remediation.postconditions import ERROR_RATE_BELOW, as_condition


class VerifyStatus(str, Enum):
    RECOVERED = "recovered"
    UNCHANGED = "unchanged"
    WORSE = "worse"


@dataclass(frozen=True)
class Outcome:
    status: VerifyStatus
    checks: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def recovered(self) -> bool:
        return self.status is VerifyStatus.RECOVERED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "checks": list(self.checks),
            "reasons": list(self.reasons),
        }


def _last_error_rate(payload: Any) -> float | None:
    """从 Prometheus matrix/vector 载荷里取出最后一个采样点。"""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    series = data.get("result")
    if not isinstance(series, list) or not series:
        return None
    values = series[0].get("values") or series[0].get("value")
    if not values:
        return None
    point = values[-1]
    try:
        return float(point[1] if isinstance(point, (list, tuple)) else point)
    except (IndexError, TypeError, ValueError):
        return None


def verify_recovery(
    plan: RemediationPlan,
    *,
    adapters: Any,
    now: datetime | None = None,
    baseline: dict[str, float] | None = None,
) -> Outcome:
    """按计划声明的后置条件重新观测一次，给出三层结论。"""
    moment = now or datetime.now(UTC)
    declared = list(plan.post_conditions)
    if not declared:
        return Outcome(
            status=VerifyStatus.UNCHANGED,
            reasons=["the action declares no post-conditions; recovery cannot be verified"],
        )

    checks: list[dict[str, Any]] = []
    unverifiable: list[str] = []
    for item in declared:
        condition = as_condition(item)
        if condition is None:
            # 自由文本不是"无需验证"，是"无法验证"
            unverifiable.append(str(item))
            continue
        if condition.kind != ERROR_RATE_BELOW:
            unverifiable.append(condition.describe())
            continue

        end = moment
        start = moment - timedelta(seconds=condition.window_seconds)
        try:
            payload = adapters.query_service_error_rate(
                condition.service, start.isoformat(), end.isoformat())
        except Exception as exc:  # 取不到数 ≠ 没恢复
            checks.append({
                "kind": condition.kind,
                "service": condition.service,
                "threshold": condition.threshold,
                "observed": None,
                "error": f"{type(exc).__name__}: {exc}",
            })
            unverifiable.append(f"{condition.describe()} (probe failed: {exc})")
            continue

        observed = _last_error_rate(payload)
        if observed is None:
            checks.append({
                "kind": condition.kind,
                "service": condition.service,
                "threshold": condition.threshold,
                "observed": None,
                "error": "no data points in the probe window",
            })
            unverifiable.append(f"{condition.describe()} (no data points)")
            continue

        checks.append({
            "kind": condition.kind,
            "service": condition.service,
            "threshold": condition.threshold,
            "observed": observed,
            "met": observed < condition.threshold,
        })

    if unverifiable:
        return Outcome(
            status=VerifyStatus.UNCHANGED,
            checks=checks,
            reasons=["cannot verify automatically: " + "; ".join(unverifiable)],
        )

    measured = [c for c in checks if c.get("observed") is not None]
    if all(c.get("met") for c in measured):
        return Outcome(status=VerifyStatus.RECOVERED, checks=checks,
                       reasons=["all post-conditions are met"])

    reference = dict(plan.baseline or {})
    reference.update(baseline or {})
    worse: list[str] = []
    for check in measured:
        before = reference.get(check.get("service") or "")
        if before is not None and check["observed"] > before:
            worse.append(
                f"{check['service']} error rate rose from {before} to {check['observed']}")
    if worse:
        return Outcome(status=VerifyStatus.WORSE, checks=checks, reasons=worse)

    missed = [f"{c['service']} error rate is {c['observed']}, threshold {c['threshold']}"
              for c in measured if not c.get("met")]
    return Outcome(status=VerifyStatus.UNCHANGED, checks=checks,
                   reasons=missed or ["no measurement was available"])


class RecoveryVerifier:
    """`execute_plan` 的 verifier 协议适配：只有 RECOVERED 才算通过。

    其他两种都返回 False —— 执行器只关心"能不能收工"，但真实原因
    通过 `last_outcome` 留给调用方记录，不会丢。
    """

    def __init__(self, adapters: Any, *, baseline: dict[str, float] | None = None,
                 now: datetime | None = None) -> None:
        self._adapters = adapters
        self._baseline = baseline
        self._now = now
        self.last_outcome: Outcome | None = None

    def verify(self, plan: RemediationPlan) -> bool:
        outcome = verify_recovery(plan, adapters=self._adapters,
                                  now=self._now, baseline=self._baseline)
        self.last_outcome = outcome
        return outcome.recovered
