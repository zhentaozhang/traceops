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

## 两种布局

| 布局 | 形状 | 来源 |
| --- | --- | --- |
| `csv` | `{service}_{metric}/{case}/data.csv`，每个 (service, metric) 一个目录 | Zenodo / figshare 的原始数据 |
| `parquet` | 一个案例一个目录，目录内 `metrics.parquet`（`time` + 每列 `{service}_{metric}`） | HuggingFace `phamquiluan/RCAEval`（zstd 压缩版，33MB 起步而非 38GB） |

两种布局都用同一套 `_resample` / `_normalise` / `column_target`，所以分数可比；
差别只在"从哪个文件读列"和"真因标签从目录名怎么解析"。

parquet 读取用**惰性导入的 pyarrow**：仓库其余部分刻意只依赖标准库 + pydantic
+ jsonschema，为了一个可选的数据集格式把 pyarrow 变成硬依赖不值得。没装时
抛 `RcaEvalDataMissing` 并明说要装什么。

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
from evaluation.scenario import (
    GroundTruth,
    Scenario,
    ServiceSignals,
    Signals,
    SlowSpan,
)

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
#: 告警延迟：`fault_start`（告警时刻）= 注入时刻 + 这个延迟。
#:
#: 为什么必须分开：告警永远**晚于**故障开始 —— 要先跑完一个评估窗口、过阈值、
#: 再走通知链路。而调查窗口的约定是"序列以 `fault_start` 收尾"。把两者当成
#: 同一个时刻，窗口就整个落在注入**之前**：留给检测器的全是故障前的平静曲线，
#: 注入之后真正的阶跃一个点都不在窗口里 —— 于是"严重程度"只能从窗口外偷取
#: （旧版本正是如此：把注入后的阶跃算成一个常数，乘到窗口里的基线上）。
#: 那样测出来的不是检测能力，只是转换的算术。
ALERT_DELAY_SECONDS = 300


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


def _read_columns(path: Path) -> tuple[list[float], dict[str, list[float | None]]]:
    """读宽表（`data.csv` 或 `metrics.parquet`）。

    CSV 路径**故意复用适配器那份**，不另写一个解析器：这一层存在的意义是
    "真实数据走与被信任数字相同的路径"，如果这里用另一套解析规则（比如把
    NaN 读成 0），同一份数据在"忠实值"与"可比分数"两条路上就会给出不同的
    观测 —— 那正好毁掉可比性。
    """
    if path.suffix == ".parquet":
        return _read_parquet(path)
    return _read_metric_rows(path)


def _read_parquet_table(path: Path):
    """读任意一张 parquet 表（pyarrow 惰性导入）。"""
    try:
        from pyarrow import parquet as arrow_parquet
    except ImportError as exc:  # pragma: no cover - 取决于本地环境
        raise RcaEvalDataMissing(
            f"reading {path} needs pyarrow (pip install pyarrow)") from exc
    return arrow_parquet.read_table(path)


def _read_parquet(path: Path) -> tuple[list[float], dict[str, list[float | None]]]:
    """读 HuggingFace 布局的 `metrics.parquet`：`time` 列 + 每指标一列。

    pyarrow 惰性导入（见模块 docstring）：只读列、不转 pandas —— 这里要的是
    与 CSV 路径相同的 `(times, columns)` 形状，不是 DataFrame。
    """
    try:
        from pyarrow import parquet as arrow_parquet
    except ImportError as exc:  # pragma: no cover - 取决于本地环境
        raise RcaEvalDataMissing(
            f"reading {path} needs pyarrow (pip install pyarrow); "
            f"or point the importer at the CSV layout instead") from exc

    table = arrow_parquet.read_table(path)
    names = [name for name in table.column_names if name != "time"]
    raw_times = table.column("time").to_pylist()
    raw_columns = {name: table.column(name).to_pylist() for name in names}
    times: list[float] = []
    columns: dict[str, list[float | None]] = {name: [] for name in names}
    for index, moment in enumerate(raw_times):
        if moment is None:
            continue
        times.append(float(moment))
        for name in names:
            value = raw_columns[name][index]
            # NaN 与 null 都表示"这一刻没观测"，不是 0。
            columns[name].append(None if value is None or value != value else float(value))
    return times, columns


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


#: 阶跃分数的两个窗口（分钟）：注入前 30 分钟当基线，注入后 10 分钟当"故障中"。
STEP_BEFORE_MINUTES = 30
STEP_AFTER_MINUTES = 10
#: MAD → 标准差的经验系数（正态假设下 1.4826·MAD ≈ σ）。
_MAD_TO_SIGMA = 1.4826


def _step_score(times: list[float], values: list[float | None], *,
                anchor: float,
                before_minutes: int = STEP_BEFORE_MINUTES,
                after_minutes: int = STEP_AFTER_MINUTES) -> float:
    """注入前后两个窗口的中位数差，用基线 MAD 归一（负向同样算异常）。

    第一版用的是"相对全窗口中位数的最大绝对偏离"，在真实数据上立刻失效：
    内存类指标（`*_mem`）在 70 分钟里单调爬升，最大偏离比真因的 cpu 阶跃还大
    两个数量级，于是"哪个指标更异常"这个问题被噪声接管，每个服务都被选中
    `*_mem`，真因指标反而落选（实测 `adservice_mem=5.6e6` vs `adservice_cpu` 的
    `1.4e3`）。阶跃分数只比较注入前后两个窗口，趋势在两边都存在、相减即消；
    再用基线的 MAD 归一，量纲不同的指标才能互相比较（cpu 用毫核、延迟用毫秒、
    内存用字节）。

    **取绝对值**：`loss` 类故障会让指标**下降**，方向不明时"绝对变化"才是异常，
    "变大"只是其中一种。
    """
    pairs = [(t, v) for t, v in zip(times, values) if v is not None]
    base = [v for t, v in pairs if anchor - before_minutes * 60 <= t < anchor]
    step = [v for t, v in pairs if anchor <= t <= anchor + after_minutes * 60]
    # 两个窗口都至少要有一点数据；少于这个量时不敢说"变了"。
    if len(base) < 2 or not step:
        return 0.0
    median_before = statistics.median(base)
    median_after = statistics.median(step)
    mad = statistics.median([abs(v - median_before) for v in base]) * _MAD_TO_SIGMA
    # 基线完全平的时刻 MAD=0；用一个相对量级兜底，避免除零把任何抖动放大成无穷。
    scale = mad if mad > 1e-9 else max(abs(median_before) * 0.01, 1e-9)
    return abs(median_after - median_before) / scale


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


def _normalise(series: list[float], *, own: float, scale_to: float) -> list[float]:
    """把一条真实序列按"相对本案例最大阶跃"缩放到我们的错误率量纲。

    规则只有一条：`PEAK * own_step / case_max_step`。于是"谁更异常"完全来自
    真实数据，被改变的只有绝对量纲。

    **除数必须是本案例的最大阶跃，不能是真因自己的阶跃**：拿真因当分母时，
    真因的曲线恒等于 `PEAK`，任何"峰值最高"的朴素基线都必然选中真因 ——
    那不是基线强，那是转换里把答案写进了幅度（实测这一版峰值基线 0.99）。
    用同一案例的统一除数，幅度排序才重新是一项**待判断**的证据。
    """
    if not series:
        return []
    if scale_to <= 0:
        return [0.0 for _ in series]
    scale = PEAK * (own / scale_to) if own > 0 else 0.0
    median = statistics.median(series)
    spread = max(abs(v - median) for v in series) or 1.0
    return [round(max(0.0, min(0.9, scale * abs(v - median) / spread)), 6) for v in series]


#: HuggingFace 目录名的 suite 前缀：`{suite}{system}_{service}_{fault}_{rep}`。
_SUITE_PREFIXES = ("re1ob", "re1ss", "re1tt", "re2ob", "re2ss", "re2tt",
                   "re3ob", "re3ss", "re3tt")

#: RCAEval 的故障名 → 我们的 fault_type。`delay` 是网络延迟注入，属于延迟劣化；
#: 其余（cpu/mem/disk/loss/socket）都表现成可用性受损。不在表里的交给适配器的
#: 弱映射，那里已经声明了"未知一律 availability_degradation"。
_FAULT_TO_TYPE = {
    "delay": "latency_degradation",
    "cpu": "availability_degradation",
    "mem": "availability_degradation",
    "disk": "availability_degradation",
    "loss": "availability_degradation",
    "socket": "availability_degradation",
}


def _fault_type(metric: str) -> str:
    return _FAULT_TO_TYPE.get(metric) or fault_type_for(metric)


def _layout_of(case_dir: Path) -> str | None:
    """`csv` / `parquet` / None（这个目录不是案例）。"""
    if (case_dir / "data.csv").exists():
        return "csv"
    if (case_dir / "metrics.parquet").exists():
        return "parquet"
    return None


def _truth_from_case_name(name: str) -> tuple[str, str]:
    """扁平布局的目录名 → (service, fault)。

    `re1ob_adservice_cpu_1` → `("adservice", "cpu")`；
    `re2tt_ts-order-service_cpu_1` → `("ts-order-service", "cpu")`。
    服务名里可以有 `-`，但**不含 `_`**（RCAEval 的命名约定），所以从右切两个
    下划线就能稳定拆出 (service, fault, repetition)。
    """
    for prefix in _SUITE_PREFIXES:
        if name.startswith(prefix + "_"):
            parts = name[len(prefix) + 1:].rsplit("_", 2)
            if len(parts) == 3 and parts[0] and parts[2].isdigit():
                return parts[0], parts[1]
            break
    raise ValueError(
        f"cannot derive (service, fault) from flat case directory {name!r}; "
        f"expected '{{suite}}_{{service}}_{{fault}}_{{repetition}}' "
        f"(suite one of {', '.join(_SUITE_PREFIXES)})")


def _case_groups(dataset_dir: Path) -> dict[str, list[Path]]:
    """按案例名分组。

    * CSV 布局：同一 case 名下每个 `{service}_{metric}` 目录一组（多个目录）；
    * parquet 布局：一个案例一个目录，组里只有一个元素。
    """
    if not dataset_dir.exists():
        raise RcaEvalDataMissing(
            f"RCAEval dataset directory not found: {dataset_dir} "
            f"(the telemetry is not vendored; download it separately)")
    groups: dict[str, list[Path]] = {}
    candidates = list(dataset_dir.glob("**/data.csv")) + list(
        dataset_dir.glob("**/metrics.parquet"))
    for path in candidates:
        case_dir = path.parent
        if _layout_of(case_dir) is None:
            continue
        bucket = groups.setdefault(case_dir.name, [])
        if case_dir not in bucket:
            bucket.append(case_dir)
    if not groups:
        raise RcaEvalDataMissing(
            f"no case directories under {dataset_dir}; expected either "
            f"'<service>_<metric>/<case>/data.csv' (Zenodo layout) or "
            f"'<case>/metrics.parquet' (HuggingFace layout)")
    return groups


def _split_dir_name(name: str) -> tuple[str, str]:
    if "_" not in name:
        raise ValueError(
            f"cannot derive (service, metric) from {name!r}; "
            f"expected the RCAEval layout '{{service}}_{{metric}}'")
    return tuple(name.rsplit("_", 1))  # type: ignore[return-value]


def _scope_services(hint: str, services) -> list[str]:
    """取证范围 = 告警服务 + 其余被观测到的服务（**不带方向**）。

    这里曾经是 `_star_edges`：为了把"排序"这件事打开，声明了一个以告警服务
    为中心的星形依赖边。方向在 `ServiceTopology` 里读作"前者依赖后者"，于是
    它实际说的是**告警服务依赖所有别的服务** —— 症状判定（R-008）随即把真因
    判成"下游某服务的症状"，124 个真实案例里 52 个被打到 rank 2-6，AC@1 从
    本可达到的 0.87 掉到 0.54。

    教训不是"星形不够准"，而是**范围与方向是两件事**：没有调用图时我们仍然
    知道这次故障波及哪些服务（标签、面板、受影响的实例），但完全不知道谁调用
    谁。所以这里只声明范围（`scope_services`），依赖边留空，症状规则保持关闭。
    """
    return [hint] + sorted(set(services) - {hint})


def load_topology(path: Path) -> list[tuple[str, str]]:
    """读一份声明的调用图。`[[caller, callee], ...]` 或 `{"edges": [[...]]}`。

    为什么要这个文件
    ----------------
    没有调用图时症状判定（R-008）必须关闭，否则会把真因判成"下游的症状"；
    可是关掉之后，一次网络类故障里"下游被打得更响"的现象就没法解释 ——
    实测这正好是剩下的那 13%（见 `评测口径` §6.7）。

    调用图是**部署事实**，不是数据集的属性：RCAEval 不提供，但任何真实部署
    都有（服务网格、Terraform、CI 里的依赖清单）。所以把它做成一个可选输入：
    给了就开症状判定并据此归因，没给就只声明范围。方向读作 `(caller, callee)`
    = "前者调用后者"，与 `ServiceTopology` 的约定一致。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    edges = raw.get("edges") if isinstance(raw, dict) else raw
    if not isinstance(edges, list):
        raise ValueError(f"{path}: expected a list of [caller, callee] pairs, or {{\"edges\": [...]}}")
    parsed: list[tuple[str, str]] = []
    for index, item in enumerate(edges):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"{path}: edge #{index} is not a [caller, callee] pair: {item!r}")
        caller, callee = item
        if not isinstance(caller, str) or not isinstance(callee, str) or not caller.strip() or not callee.strip():
            raise ValueError(f"{path}: edge #{index} must name two non-empty services: {item!r}")
        if caller == callee:
            raise ValueError(f"{path}: edge #{index} points {caller!r} at itself")
        parsed.append((caller.strip(), callee.strip()))
    if not parsed:
        raise ValueError(f"{path}: the call graph is empty; omit --topology-file instead")
    return parsed


#: 指标名 → 这条序列**测的是什么**。用途不是分类好看，而是让机制判定有依据：
#: "延迟上升"和"错误率上升"是两种不同的故障机制（见 ServiceSignals.metric_kind）。
_RESOURCE_METRICS = ("cpu", "mem", "diskio", "socket", "workload", "load", "disk")
_LATENCY_METRICS = ("latency", "duration", "response_time", "rt")


#: 判定"这条指标真的动了"的最小阶跃（单位是基线 MAD，即 σ）。
#: 3σ 是统计上的常规门槛；低于它的列算"没动"，而不是"动了一点点"。
STEP_SIGNIFICANT_SIGMA = 3.0


def metric_kind_of(metric: str) -> str:
    """由**指标名**判定序列的类别（error / latency / resource）。"""
    lowered = metric.lower()
    if any(hint in lowered for hint in _LATENCY_METRICS):
        return "latency"
    if any(hint in lowered for hint in _RESOURCE_METRICS):
        return "resource"
    return "error"


def _kind_for_service(service: str, chosen_metric: str,
                      readings_by_target: dict, anchor_epoch: float) -> str:
    """这个服务的异常**该怎么命名**（error / latency / resource）。

    规则不是"最异常的那列叫什么"，而是"是不是**只有**时延在动"：

    * 只有时延列越过 3σ、其余列都在噪声里 → `latency`（"变慢但没出错"）
    * 时延列之外还有错误/资源列越过 3σ → 按那一条的类别命名

    理由是实测出来的：RCAEval 的 `loss` / `disk` 故障也会把时延推上去，
    如果只按"最异常的那列"命名，这些案例会被一律答成延迟劣化，
    而数据集的标签是按**注入的故障**给的 —— 两种说法都对，但不是同一个问题。
    这条规则让"只有时延动"成为**必要条件**，从而只在数据真的只显示变慢时
    才声称延迟劣化。
    """
    kind = metric_kind_of(chosen_metric)
    if kind != "latency":
        return kind
    for (other_service, other_metric), entries in readings_by_target.items():
        if other_service != service or metric_kind_of(other_metric) == "latency":
            continue
        step = max((_step_score(t, v, anchor=anchor_epoch) for _o, t, v in entries), default=0.0)
        if step >= STEP_SIGNIFICANT_SIGMA:
            # 时长在动之外还有别的测量在动 → 这是"出错/资源异常"，不是纯变慢。
            return metric_kind_of(other_metric)
    return "latency"


#: 一条 span 的自耗时越过"基线多少倍"才被当成"这个服务自己慢"。
SELF_TIME_RATIO = 3.0
#: 自耗时的绝对下限（微秒）。慢 10ms 的服务不值得进证据。
SELF_TIME_FLOOR_US = 500_000
#: 每个服务最多带几条日志 / span，避免把整份日志塞进报告。
MAX_LOGS_PER_SERVICE = 5
MAX_SPANS_PER_SERVICE = 3
#: 日志里出现这些词的记录才算"错误日志"。这是**检索条件**，不是结论：
#: 它决定"要不要把这条给人看"，不决定根因。
_LOG_ERROR_HINTS = ("error", "exception", "timeout", "timed out", "fail",
                    "refused", "unavailable", "exhausted", "panic", "fatal")


def _read_logs(path: Path) -> list[tuple[float, str, str]]:
    """读 `logs.parquet` → [(epoch 秒, 容器名, 消息)]。"""
    table = _read_parquet_table(path)
    times = table.column("timestamp").to_pylist()
    names = table.column("container_name").to_pylist()
    messages = table.column("message").to_pylist()
    rows: list[tuple[float, str, str]] = []
    for stamp, name, message in zip(times, names, messages):
        if stamp is None or not message:
            continue
        rows.append((float(stamp), str(name or ""), str(message)))
    return rows


def _read_spans(path: Path) -> list[dict[str, object]]:
    """读 `traces.parquet` → span 列表，并算出**自耗时**。

    自耗时 = 本 span 时长 − 所有直接子 span 时长之和。父 span 的时长包含子 span，
    所以"上游被打得更响"在总时长上永远成立；只有自耗时能回答"慢在谁身上"。
    这正是 OpenRCA FAQ 里那条经验：网络类故障只看 KPI 看不出来，要看父子 span 延迟。
    """
    table = _read_parquet_table(path)
    spans: list[dict[str, object]] = []
    children: dict[tuple[str, str], int] = {}
    for trace, span_id, parent, start, duration, service, operation, method in zip(
        table.column("traceID").to_pylist(),
        table.column("spanID").to_pylist(),
        table.column("parentSpanID").to_pylist(),
        table.column("startTime").to_pylist(),
        table.column("duration").to_pylist(),
        table.column("serviceName").to_pylist(),
        table.column("operationName").to_pylist(),
        table.column("methodName").to_pylist(),
    ):
        if start is None or duration is None or not service:
            continue
        key = (str(trace), str(parent))
        span = {
            "at": float(start) / 1e6,          # startTime 是微秒纪元
            "duration_us": float(duration),    # duration 也是微秒
            "service": str(service),
            "name": str(operation or method or "span"),
            "trace": str(trace),
            "span": str(span_id),
            "parent": str(parent),
        }
        spans.append(span)
        children[key] = children.get(key, 0) + int(span["duration_us"])
    for span in spans:
        # 子 span 按 (traceID, parentSpanID) 归组；某个 span 的"子"就是
        # parentSpanID 等于它自己 spanID 的那些 —— 只减直接子，不能减整棵子树。
        child_total = children.get((span["trace"], span["span"]), 0)
        span["self_us"] = max(0.0, float(span["duration_us"]) - child_total)
    return spans


def _slow_spans_and_logs(
    case_dir: Path, *, anchor_epoch: float,
) -> tuple[dict[str, list[SlowSpan]], dict[str, list[str]]]:
    """从真实 logs / traces 里取"这个服务自己慢"的证据与错误日志。

    只认**自耗时**越过基线的 span：上游调用的总时长天然包含下游，
    拿总时长排序会把最上游的服务排在第一位，而那只是"它等了别人"。
    """
    spans_by_service: dict[str, list[SlowSpan]] = {}
    logs_by_service: dict[str, list[str]] = {}
    traces_path = case_dir / "traces.parquet"
    logs_path = case_dir / "logs.parquet"
    if traces_path.exists():
        try:
            spans = _read_spans(traces_path)
        except RcaEvalDataMissing:
            spans = []
        base: dict[str, list[float]] = {}
        after: dict[str, list[dict[str, object]]] = {}
        for span in spans:
            service = str(span["service"])
            if float(span["at"]) < anchor_epoch:
                base.setdefault(service, []).append(float(span["self_us"]))
            elif float(span["at"]) < anchor_epoch + STEP_AFTER_MINUTES * 60:
                after.setdefault(service, []).append(span)
        for service, rows in after.items():
            baseline = _median(base.get(service, []))
            threshold = max(SELF_TIME_FLOOR_US, SELF_TIME_RATIO * baseline)
            slow = [row for row in rows if float(row["self_us"]) >= threshold]
            if not slow:
                continue
            slow.sort(key=lambda row: float(row["self_us"]), reverse=True)
            picked: list[SlowSpan] = []
            seen: set[str] = set()
            for row in slow:
                name = str(row["name"])
                if name in seen:
                    continue
                seen.add(name)
                picked.append(SlowSpan(name=name, duration_ms=round(float(row["duration_us"]) / 1000.0, 3)))
                if len(picked) >= MAX_SPANS_PER_SERVICE:
                    break
            if picked:
                spans_by_service[service] = picked
    if logs_path.exists():
        try:
            rows = _read_logs(logs_path)
        except RcaEvalDataMissing:
            rows = []
        for stamp, container, message in rows:
            if not (anchor_epoch <= stamp < anchor_epoch + STEP_AFTER_MINUTES * 60):
                continue
            lowered = message.lower()
            if not any(hint in lowered for hint in _LOG_ERROR_HINTS):
                continue
            bucket = logs_by_service.setdefault(container, [])
            if message not in bucket and len(bucket) < MAX_LOGS_PER_SERVICE:
                bucket.append(message)
    return spans_by_service, logs_by_service


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def import_case(case_dirs: list[Path], *, dataset: str,
                topology: list[tuple[str, str]] | None = None) -> ImportedCase:
    """把同一案例名下的若干 `{service}_{metric}` 目录合成一个场景。

    RCAEval 的 CSV 是**宽表**：同一个 `case` 里，别的组件的指标也作为列出现。
    于是同一服务可能被两条路径贡献（它自己的目录、以及别人的宽表列）。规则：

    * 有自己目录的服务，**用自己目录那一列**（专属记录，口径一致）；
    * 只出现在别人宽表里的服务，用那一条（二手列），并单独计数。
    """
    readings: list[tuple[str, str, Path, list[float], dict[str, list[float | None]]]] = []
    inject_time: datetime | None = None
    layouts: set[str] = set()
    for case_dir in case_dirs:
        layout = _layout_of(case_dir) or "csv"
        if layout == "csv":
            service, metric = _split_dir_name(case_dir.parent.name)
            times, columns = _read_columns(case_dir / "data.csv")
        else:
            service, metric = _truth_from_case_name(case_dir.name)
            times, columns = _read_columns(case_dir / "metrics.parquet")
        layouts.add(layout)
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

    # 锚点（注入时刻）要在算分数之前定下来：阶跃分数以它为界切两个窗口。
    anchor_epoch = (inject_time.timestamp() if inject_time
                    else max(max(times) for _s, _m, _d, times, _c in readings))
    # 观测窗口右端 = 告警时刻 = 注入时刻 + 告警延迟。序列以它收尾，
    # 于是窗口里**包含注入之后的真实数据**（见 ALERT_DELAY_SECONDS）。
    watch_anchor = anchor_epoch + ALERT_DELAY_SECONDS

    deviations = {
        target: max(
            (_step_score(times, values, anchor=anchor_epoch)
             for _owned, times, values in entries),
            default=0.0,
        )
        for target, entries in readings_by_target.items()
    }

    # 真因：单目录案例直接信目录名（那就是 RCAEval 的标签）；多目录案例里
    # 目录名不再是单一答案，于是取"偏离最大的 (service, metric)"——数据自己说的。
    if len(readings) == 1:
        truth_key = (readings[0][0], readings[0][1])
    else:
        truth_key = max(deviations, key=lambda key: deviations[key])
    # 本案例的统一除数：所有服务除以同一个数，幅度才有可比性。
    case_scale = max(deviations.values(), default=0.0) or 1.0

    per_service: dict[str, ServiceSignals] = {}
    strengths: dict[str, float] = {}
    for (service, metric), entries in sorted(readings_by_target.items()):
        owned = [entry for entry in entries if entry[0]]
        chosen = (owned or entries)[0]
        _owned, times, values = chosen
        series = _resample(times, values, anchor=watch_anchor)
        if not series:
            continue
        own = max(deviations[(service, metric)], max(
            (_step_score(t, v, anchor=anchor_epoch) for _o, t, v in entries), default=0.0))
        strengths[service] = max(strengths.get(service, 0.0), own)
        candidate = ServiceSignals(
            metric_kind=_kind_for_service(
                service, metric, readings_by_target, anchor_epoch),
            error_rate_series=_normalise(series, own=own, scale_to=case_scale))
        existing = per_service.get(service)
        # 同一服务多条指标时，留"更异常"的那条：我们的证据里每个服务只有一条曲线。
        # 代价是"同一个服务既变慢又出错"只能保留一半——这写进 description。
        if existing is None or max(candidate.error_rate_series) > max(existing.error_rate_series):
            per_service[service] = candidate

    # 真实日志与链路：只在案例目录里确实有这两份文件时才用。
    # 它们回答的是指标回答不了的问题 ——"慢在谁自己身上"（自耗时）。
    spans_by_service: dict[str, list[SlowSpan]] = {}
    logs_by_service: dict[str, list[str]] = {}
    for case_dir in dict.fromkeys(case_dirs):
        found_spans, found_logs = _slow_spans_and_logs(case_dir, anchor_epoch=anchor_epoch)
        for service, spans in found_spans.items():
            spans_by_service.setdefault(service, spans)
        for service, messages in found_logs.items():
            logs_by_service.setdefault(service, messages)
    for service, spans in spans_by_service.items():
        entry = per_service.get(service)
        if entry is not None:
            entry.slow_spans = spans
    for service, messages in logs_by_service.items():
        entry = per_service.get(service)
        if entry is not None:
            entry.error_logs = messages

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

    fault_start = datetime.fromtimestamp(watch_anchor, tz=UTC)
    window_start = fault_start - timedelta(seconds=GRID_SECONDS * GRID_POINTS)
    window_end = fault_start

    notes = [
        f"source: RCAEval {dataset}/{truth_service}_{truth_metric}/{case_dirs[0].name}",
        f"layout: {'+'.join(sorted(layouts))}",
        f"magnitude: the case's largest step maps to {PEAK} "
        "(one divisor per case, so relative strength comes from the raw data; "
        "units are not preserved)",
        "timestamps: naive RCAEval times are interpreted as UTC+8 per their FAQ",
        f"alert delay: fault_start is {ALERT_DELAY_SECONDS // 60} min after inject_time, "
        "so the window ends after the fault began (an alert never fires before the "
        "fault); the step itself is measured around inject_time",
        "grid: resampled to the harness grid by nearest neighbour, no interpolation",
        "metric kinds: each service keeps its most anomalous column, tagged with the "
        "kind the column measures (error / latency / resource); a service that both "
        "slowed down and errored keeps only the louder half",
        "scope: RCAEval ships no call graph, so the affected services are "
        "declared as a scope list with no edges: every observed service is "
        "investigated, and no dependency direction is invented (symptom "
        "demotion stays off because we do not know who calls whom)",
    ]
    if inject_time is None:
        notes.append("inject_time.txt missing: fault_start taken from the end of the data")
    if len(readings) == 1:
        notes.append("truth: taken from the case directory name (RCAEval's own label)")
    if topology:
        notes.append(
            f"topology: {len(topology)} declared edges were supplied, so a candidate "
            "whose dependency is failing can be demoted as a symptom (R-008 is on)")

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
            fault_type=_fault_type(truth_metric),
            expected_diagnosis="SUPPORTED",
        ),
        topology_edges=list(topology or []),
        scope_services=[] if topology else _scope_services(truth_service, per_service),
        scope_depth=1,
    )
    return ImportedCase(
        scenario=scenario, dataset=dataset, service=truth_service,
        metric=truth_metric, case_id=case_dirs[0].name,
        candidate_pool=(truth_service, *distractors), notes=tuple(notes),
    )


def import_dataset(dataset_dir: Path, *, dataset: str | None = None,
                   limit: int | None = None,
                   topology: list[tuple[str, str]] | None = None) -> list[ImportedCase]:
    """导入整个数据集目录。按案例名分组，逐组导入。"""
    name = dataset or dataset_dir.name
    groups = _case_groups(dataset_dir)
    imported: list[ImportedCase] = []
    skipped: list[str] = []
    for case_id in sorted(groups):
        if limit is not None and len(imported) >= limit:
            break
        try:
            imported.append(import_case(groups[case_id], dataset=name, topology=topology))
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
