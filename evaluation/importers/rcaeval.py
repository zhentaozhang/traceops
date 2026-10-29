"""把 RCAEval 的真实案例导入成我们的 `Scenario`。

为什么还要这一层
----------------
`evaluation/adapters/rcaeval_adapter.py` 已经能把 RCAEval 的 CSV 转成 `Evidence`
并跑一次真实调查 —— 那条路是"忠实原始值"的，用来回答"这套系统在真实数据上
能不能出结论"。但它有一个致命的评估缺陷：**它自己算分**。数字与 12 个合成
场景的 AC@k 不可比，也进不了漏斗拆解、朴素基线和盲测包。

这一层把同一批案例转成 `Scenario`，于是：

* 同一套 `harness` 造数据源载荷（Prometheus matrix 形状，不是我们的内部结构）；
* 同一套 `metrics.aggregate` 算 AC@k / MRR / 引用精确率；
* 同一套 `decomposition` 做漏斗拆解与朴素基线；
* 同一套 `blind.export_pack` 导出盲测包，交给别人复算。

一句话：**真实数据必须和被信任的数字走同一条打分路径**，否则它只是另一份自测。

## RCAEval 的数据布局（实读其 `utility/__init__.py` 的 `get_data_dirs`）

```
data/{dataset}/{service}_{metric}/{case}/
    data.csv         # 宽表：第一列 time（epoch 秒），其余列每指标一列
    inject_time.txt  # 故障注入时刻
```

* 目录名 `{service}_{metric}` **自带 ground truth**：被注入故障的就是这个服务；
* `metric ∈ {cpu, mem, socket, workload, latency-90, error, diskio}`；
* 未观测处是 NaN，官方明确**不 forward-fill**；
* 官方 FAQ 明确：**所有时间戳按 UTC+8 记录**。

## 三处必须说明的转换（不说明就等于偷偷改题）

1. **时间**：`inject_time.txt` 里若是无时区的字符串，按 UTC+8 解释再转 UTC。
   不这样做会整体错 8 小时，而"最早异常时间"是我们的答案三元组之一。
2. **样本网格**：RCAEval 的采样间隔不固定（有的 1 分钟、有的 5 分钟），
   我们的 harness 按 `STEP_SECONDS = 60` 对齐到 `fault_start`。导入时用
   **最近邻重采样**（容差 3 个原始间隔），超出容差的点宁可缺，也不插值 ——
   插值会凭空造出一段"指标平稳"的观测。
3. **量纲**：我们的错误率阈值是 0.05，而 RCAEval 的列是 CPU 百分比、延迟毫秒、
   socket 数。必须归一化，否则"任何列都超阈值"。规则是**每案例一个常数**：
   真因服务的峰值映射到 `PEAK = 0.35`，其它服务按**它们真实的相对偏离**缩放
   (`0.35 * own_deviation / truth_deviation`)。相对的"谁更异常"完全来自真实数据，
   被改变的只有绝对量纲 —— 这一点写进 `Scenario.description`，跟着案例一起走。

## 边界

* 数据集不随仓库分发（HuggingFace 上约 50GB）。目录不存在时抛
  `RcaEvalDataMissing`，**不返回空**：空列表会让"没下载"看起来像"这案例没异常"。
* 只导入指标类别。RCAEval 的 telecom/bank 子集另有日志与 trace 目录，
  形状不一致，没验证过就不假装支持（`load_case_dirs` 会跳过并在报告里说明）。
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from evaluation.adapters.rcaeval_adapter import (
    METRIC_HINTS,
    RcaEvalDataMissing,
    _parse_inject_time,
    _read_metric_rows,
    fault_type_for,
)
from evaluation.scenario import GroundTruth, Scenario, ServiceSignals, Signals

#: 目标网格间距，与 `evaluation.harness.STEP_SECONDS` 保持一致。
GRID_SECONDS = 60
#: 网格点数：故障前 30 分钟。RCAEval 的观察窗普遍比这长，取尾段就够。
GRID_POINTS = 30
#: 真因服务峰值映射到多少（必须显著高于 `ERROR_RATE_THRESHOLD = 0.05`）。
PEAK = 0.35
#: 非真因服务里"最像真因"的两个进入候选池；再多只会稀释并列判断。
MAX_DISTRACTORS = 2
#: 最近邻重采样容差 = 原始间隔 × 该倍数。超出就说"那一刻没观测"。
RESAMPLE_TOLERANCE = 3


@dataclass(frozen=True)
class ImportedCase:
    """一个导入结果：场景 + 它来自哪里、做了哪些转换。"""

    scenario: Scenario
    dataset: str
    service: str
    metric: str
    case_id: str
    candidate_pool: tuple[str, ...]
    notes: tuple[str, ...]


def _read_columns(data_csv: Path) -> tuple[list[float], dict[str, list[float | None]]]:
    """读宽表。**故意复用适配器那份**，不另写一个解析器。

    理由：这一层存在的意义是"真实数据走与被信任数字相同的路径"，
    如果这里用另一套解析规则（比如把 NaN 读成 0），同一份 CSV 在
    "忠实值"与"可比分数"两条路上就会给出不同的观测 —— 那正好毁掉可比性。
    """
    return _read_metric_rows(data_csv)


def _column_of(columns: dict[str, list[float | None]], service: str) -> list[float | None] | None:
    """挑出属于这个服务的列。列名形如 `order-service_cpu`；单列文件直接用它。"""
    if len(columns) == 1:
        return next(iter(columns.values()))
    for name, values in columns.items():
        if name.startswith(f"{service}_") or name == service:
            return values
    return None


def column_target(column: str, *, dir_service: str, dir_metric: str) -> tuple[str, str] | None:
    """宽表的列名 → (service, metric)。

    RCAEval 的列名是 `{service}_{metric}`，与目录名同一套规则；但列存在宽表里，
    只有目录名能告诉我们"这一列属于谁"是不成立的 —— 别人的表里也有我的列。
    所以按已知 metric 后缀切，切不出来就退回最后一个下划线。
    """
    if column == dir_service:
        return (dir_service, dir_metric)
    for metric in sorted(METRIC_HINTS, key=len, reverse=True):
        suffix = f"_{metric}"
        if column.endswith(suffix) and len(column) > len(suffix):
            return (column[: -len(suffix)], metric)
    if "_" in column:
        service, metric = column.rsplit("_", 1)
        return (service, metric)
    return None


def _deviation(values: Iterable[float | None]) -> float:
    """相对正常水平的偏离（用中位数当正常水平，抗偶发尖峰）。"""
    present = [v for v in values if v is not None]
    if len(present) < 3:
        return 0.0
    median = statistics.median(present)
    return max(abs(v - median) for v in present)


def _resample(times: list[float], values: list[float | None],
              *, anchor: float) -> list[float]:
    """把原始 (time, value) 对齐到以 anchor 结尾、间距 GRID_SECONDS 的网格。

    最近邻，且**只接受容差内的邻居**：RCAEval 明确不做 forward-fill，
    我们插值就等于自己造数据。缺的点直接不产出 —— 序列因此可能变短，
    这是诚实的（`error_rate_series` 越短，说明那一刻确实没观测）。
    """
    if not times:
        return []
    spacing = statistics.median(
        [b - a for a, b in zip(times, times[1:]) if b > a] or [GRID_SECONDS])
    tolerance = max(float(spacing) * RESAMPLE_TOLERANCE, GRID_SECONDS)
    ordered = sorted(zip(times, values), key=lambda pair: pair[0])
    grid = [anchor - GRID_SECONDS * (GRID_POINTS - 1 - i) for i in range(GRID_POINTS)]
    out: list[float] = []
    for target in grid:
        best = min(ordered, key=lambda pair: abs(pair[0] - target))
        if abs(best[0] - target) > tolerance or best[1] is None:
            continue
        out.append(float(best[1]))
    return out


def _normalise(series: list[float], *, own: float, truth: float) -> list[float]:
    """把一条真实序列按"相对真因的偏离"缩放到我们的错误率量纲。

    规则只有一条：`PEAK * own_deviation / truth_deviation`。于是
    "谁更异常"完全来自真实数据，被改变的只有绝对量纲。
    """
    if not series:
        return []
    if truth <= 0:
        return [0.0 for _ in series]
    scale = PEAK * (own / truth) if own > 0 else 0.0
    median = statistics.median(series)
    spread = max(abs(v - median) for v in series) or 1.0
    return [round(max(0.0, min(0.9, scale * abs(v - median) / spread)), 6) for v in series]


def _case_groups(dataset_dir: Path) -> dict[str, list[Path]]:
    """按案例名分组：同一个 case 名下每个 (service_metric) 是一个目录。"""
    if not dataset_dir.exists():
        raise RcaEvalDataMissing(
            f"RCAEval dataset directory not found: {dataset_dir} "
            f"(the dataset is ~50GB and is not vendored; download it separately)")
    groups: dict[str, list[Path]] = {}
    for data_csv in dataset_dir.glob("**/data.csv"):
        case_dir = data_csv.parent
        groups.setdefault(case_dir.name, []).append(case_dir)
    if not groups:
        raise RcaEvalDataMissing(
            f"no '<service>_<metric>/<case>/data.csv' under {dataset_dir}; "
            f"expected the RCAEval layout")
    return groups


def _split_dir_name(name: str) -> tuple[str, str]:
    if "_" not in name:
        raise ValueError(
            f"cannot derive (service, metric) from {name!r}; "
            f"expected the RCAEval layout '{{service}}_{{metric}}'")
    return tuple(name.rsplit("_", 1))  # type: ignore[return-value]


def import_case(case_dirs: list[Path], *, dataset: str) -> ImportedCase:
    """把同一案例名下的若干 `{service}_{metric}` 目录合成一个场景。

    RCAEval 的 CSV 是**宽表**：同一个 `case` 里，别的组件的指标也作为列出现。
    于是同一服务可能被两条路径贡献（它自己的目录、以及别人的宽表列）。规则：

    * 有自己目录的服务，**用自己目录那一列**（专属记录，口径一致）；
    * 只出现在别人宽表里的服务，用那一条（二手列），并单独计数。
    """
    readings: list[tuple[str, str, Path, list[float], dict[str, list[float | None]]]] = []
    inject_time: datetime | None = None
    for case_dir in case_dirs:
        service, metric = _split_dir_name(case_dir.parent.name)
        times, columns = _read_columns(case_dir / "data.csv")
        if not times:
            continue
        if inject_time is None:
            inject_time = _parse_inject_time(case_dir / "inject_time.txt")
        readings.append((service, metric, case_dir, times, columns))

    if not readings:
        raise RcaEvalDataMissing(f"case {case_dirs[0].name!r} has no readable data.csv")

    # 每个 (service, metric) 可能有多份读数；记住哪份是"自己的目录"。
    readings_by_target: dict[tuple[str, str], list[tuple[bool, list[float], list[float | None]]]] = {}
    for service, metric, case_dir, times, columns in readings:
        for column, values in columns.items():
            target = column_target(column, dir_service=service, dir_metric=metric)
            if target is None:
                continue
            owned = case_dir.parent.name.startswith(f"{target[0]}_") and (
                case_dir.parent.name == column or case_dir.parent.name == f"{target[0]}_{target[1]}")
            readings_by_target.setdefault(target, []).append((owned, times, values))

    if not readings_by_target:
        raise RcaEvalDataMissing(f"case {case_dirs[0].name!r} has no usable columns")

    deviations = {
        target: max((_deviation(values) for _owned, _times, values in entries), default=0.0)
        for target, entries in readings_by_target.items()
    }

    # 真因：单目录案例直接信目录名（那就是 RCAEval 的标签）；多目录案例里
    # 目录名不再是单一答案，于是取"偏离最大的 (service, metric)"——数据自己说的。
    if len(readings) == 1:
        truth_key = (readings[0][0], readings[0][1])
    else:
        truth_key = max(deviations, key=lambda key: deviations[key])
    truth_deviation = deviations.get(truth_key) or 1.0

    anchor_epoch = (inject_time.timestamp() if inject_time
                    else max(max(times) for _s, _m, _d, times, _c in readings))

    per_service: dict[str, ServiceSignals] = {}
    strengths: dict[str, float] = {}
    for (service, metric), entries in sorted(readings_by_target.items()):
        owned = [entry for entry in entries if entry[0]]
        chosen = (owned or entries)[0]
        _owned, times, values = chosen
        series = _resample(times, values, anchor=anchor_epoch)
        if not series:
            continue
        own = max(deviations[(service, metric)], max(
            (_deviation(v) for _o, _t, v in entries), default=0.0))
        strengths[service] = max(strengths.get(service, 0.0), own)
        candidate = ServiceSignals(
            error_rate_series=_normalise(series, own=own, truth=truth_deviation))
        existing = per_service.get(service)
        # 同一服务多条指标时，留"更异常"的那条：我们的错误率只有一条曲线。
        if existing is None or max(candidate.error_rate_series) > max(existing.error_rate_series):
            per_service[service] = candidate

    truth_service, truth_metric = truth_key
    if not per_service:
        observed = (min(min(times) for _s, _m, _d, times, _c in readings),
                    max(max(times) for _s, _m, _d, times, _c in readings))
        if inject_time is not None and not observed[0] <= anchor_epoch <= observed[1]:
            # 注入时刻与观测窗口相隔太远：多半是时区解释错了，或者 inject_time
            # 与 data.csv 不是同一份案例。两种都不该悄悄产出一个空场景。
            raise RcaEvalDataMissing(
                f"case {case_dirs[0].name!r}: inject_time "
                f"{datetime.fromtimestamp(anchor_epoch, tz=UTC).isoformat()} lies outside the "
                f"observations "
                f"[{datetime.fromtimestamp(observed[0], tz=UTC).isoformat()}, "
                f"{datetime.fromtimestamp(observed[1], tz=UTC).isoformat()}]; "
                f"refusing to build a scenario whose fault_start is not covered by data")
        raise RcaEvalDataMissing(f"case {case_dirs[0].name!r} produced no usable series")

    # 候选池：真因 + 偏离最大的若干干扰项。干扰项用它们**真实**的偏离强度，
    # 所以"看起来也异常但其实不是"这件事是数据给的，不是我们安排的。
    distractors = sorted(
        (service for service in per_service if service != truth_service),
        key=lambda service: strengths.get(service, 0.0), reverse=True,
    )[:MAX_DISTRACTORS]

    fault_start = datetime.fromtimestamp(anchor_epoch, tz=UTC)
    window_start = fault_start - timedelta(seconds=GRID_SECONDS * GRID_POINTS)
    window_end = fault_start + timedelta(seconds=GRID_SECONDS * 2)

    notes = [
        f"source: RCAEval {dataset}/{truth_service}_{truth_metric}/{case_dirs[0].name}",
        "magnitude: rescaled so the truth's peak is "
        f"{PEAK} (relative strength comes from the raw data; units are not preserved)",
        "timestamps: naive RCAEval times are interpreted as UTC+8 per their FAQ",
        "grid: resampled to the harness grid by nearest neighbour, no interpolation",
    ]
    if inject_time is None:
        notes.append("inject_time.txt missing: fault_start taken from the end of the data")
    if len(readings) == 1:
        notes.append("truth: taken from the case directory name (RCAEval's own label)")

    scenario = Scenario(
        scenario_id=f"rcaeval-{dataset}-{truth_service}-{truth_metric}-{case_dirs[0].name}",
        name=f"{dataset}: {truth_service}/{truth_metric} @ {case_dirs[0].name}",
        description="; ".join(notes),
        service_hint=truth_service,
        fault_start=fault_start,
        window_start=window_start,
        window_end=window_end,
        signals=Signals(per_service=per_service),
        ground_truth=GroundTruth(
            service=truth_service,
            component=None,
            fault_type=fault_type_for(truth_metric),
            expected_diagnosis="SUPPORTED",
        ),
        topology_edges=[],
        scope_depth=1,
    )
    return ImportedCase(
        scenario=scenario, dataset=dataset, service=truth_service,
        metric=truth_metric, case_id=case_dirs[0].name,
        candidate_pool=(truth_service, *distractors), notes=tuple(notes),
    )


def import_dataset(dataset_dir: Path, *, dataset: str | None = None,
                   limit: int | None = None) -> list[ImportedCase]:
    """导入整个数据集目录。按案例名分组，逐组导入。"""
    name = dataset or dataset_dir.name
    groups = _case_groups(dataset_dir)
    imported: list[ImportedCase] = []
    skipped: list[str] = []
    for case_id in sorted(groups):
        if limit is not None and len(imported) >= limit:
            break
        try:
            imported.append(import_case(groups[case_id], dataset=name))
        except (RcaEvalDataMissing, ValueError) as exc:
            # 一个案例坏了不该让整批导入失败，但**原因必须留下来**：
            # 只报"没有可导入的案例"等于把"时区解释错了"和"目录是空的"
            # 说成同一件事。
            skipped.append(f"{case_id}: {exc}")
            continue
    if not imported:
        detail = "; ".join(skipped[:5]) or "no cases were found"
        raise RcaEvalDataMissing(f"no importable cases under {dataset_dir}: {detail}")
    return imported


def write_scenarios(imported: Iterable[ImportedCase], out_dir: Path) -> list[Path]:
    """写成 `evaluation/scenarios/` 认得的 JSON（一个案例一个文件）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in imported:
        path = out_dir / f"{item.scenario.scenario_id}.json"
        path.write_text(
            json.dumps(item.scenario.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8")
        written.append(path)
    return written


def summarise(imported: Iterable[ImportedCase]) -> dict[str, Any]:
    items = list(imported)
    pools = [len(item.candidate_pool) for item in items]
    return {
        "cases": len(items),
        "datasets": sorted({item.dataset for item in items}),
        "services": len({item.service for item in items}),
        "metrics": sorted({item.metric for item in items}),
        "candidate_pool_mean": round(sum(pools) / len(pools), 2) if pools else 0.0,
        "candidate_pool_max": max(pools) if pools else 0,
        "single_candidate_cases": sum(1 for size in pools if size == 1),
    }
