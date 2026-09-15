"""RCAEval 数据集适配器：把外部基准转换成 TraceOps 观测格式。

## 为什么需要（PRD 里 P1 的「RCAEval 接入」）

TraceOps 的 11 个场景全是**自己写的合成数据**，因此存在"规则自证"风险
（`评测口径 §0` 已诚实记录）。要判断真实能力，必须跑外部基准。

蓝图的架构要求（§10）明确：

> 数据集适配器把各基准数据转换成 TraceOps 观测格式（**不伪装成 Grafana 数据源**），
> 离线基准测试与真实诊断共用 RCA Engine、Evidence Validator 和 Report Schema。

"不伪装成数据源"是关键：适配器产出的必须是 `Evidence`，走与真实调查
**完全相同**的归一化与校验路径，否则测出来的不是同一套系统。

## RCAEval 的数据契约（实读 `RCAEval/utility/__init__.py:80`）

```
data/{dataset}/{service}_{metric}/{case}/
    data.csv        # 宽表：time(秒) + 每指标一列 float
    inject_time.txt # 故障注入时刻
```

- 时间点是**并集**，未观测处保持 NaN，**不 forward-fill**
- 指标列名 = `<service>_<metric>`，metric ∈ {cpu, mem, socket, workload,
  latency-90, error, diskio}
- 目录名 `{service}_{metric}` **自带 ground truth**（`main.py:283` 这样解析）

## 本适配器的边界

它只做**格式转换**，不做诊断。产出 `RcaEvalCase`，其 `evidence` 可以直接
喂给 `run_investigation`。真实下载数据（HuggingFace 约 50GB）不在本仓库内，
所以适配器必须能在**给定本地目录缺失时**给出清晰错误，而不是静默返回空。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from traceops.domain.models import Evidence, EvidenceType
from traceops.evidence.store import EvidenceStore

# RCAEval 的指标列后缀 → TraceOps 的故障机制提示。
# 注意这是**弱映射**：RCAEval 的标签粒度是 (service, metric)，
# 而 TraceOps 的 fault_type 是机制名，两套词汇表并不一一对应。
# 因此跨基准只应比较**服务级 AC@k**，不比较 fault_type（见 评测口径）。
METRIC_HINTS: dict[str, str] = {
    "cpu": "availability_degradation",
    "mem": "availability_degradation",
    "socket": "availability_degradation",
    "diskio": "availability_degradation",
    "workload": "availability_degradation",
    "error": "availability_degradation",
    "latency": "latency_degradation",
    "latency-90": "latency_degradation",
}


class RcaEvalDataMissing(FileNotFoundError):
    """数据目录不存在。

    **宁可报错也不要返回空**：返回空会让"没有数据"看起来像"这个案例没有异常"，
    进而在评估里被当成一次失败的诊断 —— 那是把环境问题算成能力问题。
    """


@dataclass
class RcaEvalCase:
    """一个 RCAEval 案例转换后的形态。"""

    dataset: str
    service: str
    metric: str
    case_id: str
    inject_time: datetime | None
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def ground_truth_service(self) -> str:
        return self.service

    def to_observations(self) -> dict[str, Any]:
        """诊断所需的观测摘要（不含 ground truth，防止泄漏进调查入参）。"""
        return {
            "dataset": self.dataset,
            "service_hint": self.service,
            "case_id": self.case_id,
            "evidence_count": len(self.evidence),
        }


def _parse_inject_time(path: Path) -> datetime | None:
    """`inject_time.txt` 里是一个 unix 秒或 ISO 时间。读不出时返回 None。"""
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=UTC)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _read_metric_rows(data_csv: Path) -> tuple[list[float], dict[str, list[float | None]]]:
    """读宽表 CSV：返回 (times, {列名: 值列表})。

    不用 pandas：`pyproject.toml` 刻意只依赖标准库 + pydantic + jsonschema。
    RCAEval 官方实现用 pandas+pyarrow，但适配器只需读 CSV，标准库足够。
    """
    times: list[float] = []
    columns: dict[str, list[float | None]] = {}
    with data_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_time = row.get("time")
            if raw_time is None:
                continue
            try:
                times.append(float(raw_time))
            except ValueError:
                continue
            for name, value in row.items():
                if name == "time" or name is None:
                    continue
                columns.setdefault(name, [])
                if value in (None, "", "NaN", "nan"):
                    columns[name].append(None)
                    continue
                try:
                    columns[name].append(float(value))
                except ValueError:
                    columns[name].append(None)
    return times, columns


def _series_from_column(times: list[float], values: list[float | None]) -> list[list[float]]:
    """一对 (time, value) → Prometheus matrix 的 values 形式，跳过 NaN。

    跳过 NaN 而不是填 0：RCAEval 明确"未观测保持 NaN，不 forward-fill"，
    把它当 0 会凭空制造一段"指标正常"的观测。
    """
    out: list[list[float]] = []
    for ts, value in zip(times, values):
        if value is None:
            continue
        out.append([ts, value])
    return out


def load_case(case_dir: Path, *, dataset: str) -> RcaEvalCase:
    """把一个 RCAEval 案例目录转换成 Evidence 列表。

    Args:
        case_dir: `data/{dataset}/{service}_{metric}/{case}` 这一层。

    Raises:
        RcaEvalDataMissing: 目录或 data.csv 不存在。
    """
    data_csv = case_dir / "data.csv"
    if not data_csv.exists():
        raise RcaEvalDataMissing(f"RCAEval case has no data.csv: {case_dir}")

    # 目录名自带 ground truth：上一级是 "{service}_{metric}"
    parent_name = case_dir.parent.name
    if "_" not in parent_name:
        raise ValueError(
            f"cannot derive (service, metric) from {parent_name!r}; "
            "expected the RCAEval layout '{{service}}_{{metric}}'")
    service, metric = parent_name.rsplit("_", 1)

    inject_time = _parse_inject_time(case_dir / "inject_time.txt")
    times, columns = _read_metric_rows(data_csv)

    store = EvidenceStore()
    store.register_source("rcaeval")
    evidence: list[Evidence] = []

    if not times:
        return RcaEvalCase(dataset=dataset, service=service, metric=metric,
                           case_id=case_dir.name, inject_time=inject_time)

    window_start = datetime.fromtimestamp(min(times), tz=UTC)
    window_end = datetime.fromtimestamp(max(times), tz=UTC)
    # observed_at 取窗口末端：README 的约定是"观测时间不晚于取回时刻"，
    # 单点观测没有更精确的时间戳可用。
    observed_at = window_end

    for column, values in columns.items():
        values_series = _series_from_column(times, values)
        if not values_series:
            continue  # 全 NaN 的列不是观测（RCAEval 明确不做 forward-fill）
        # 列名形如 "order-service_cpu"：取服务前缀作为证据归属
        owner = column.rsplit("_", 1)[0] if "_" in column else service
        evidence.append(
            Evidence(
                incident_id=f"rcaeval-{dataset}-{case_dir.name}",
                investigation_run_id=f"rcaeval-{dataset}-{case_dir.name}",
                evidence_type=EvidenceType.METRIC,
                source_id="rcaeval",
                service_id=owner,
                observed_at=observed_at,
                window_start=window_start,
                window_end=window_end,
                query_template_id=f"qt-rcaeval-{metric}",
                query_params={"column": column, "_tier": "investigation"},
                rendered_query=f"rcaeval:{dataset}/{service}_{metric}/{case_dir.name}/{column}",
                normalized_data={
                    "kind": "metric",
                    "result_type": "matrix",
                    "series": [{"metric": {"service": owner, "column": column},
                                "values": values_series}],
                    "result_count": 1,
                    "total_records": len(values_series),
                    "truncated": False,
                },
                checksum="",
            )
        )

    return RcaEvalCase(dataset=dataset, service=service, metric=metric,
                       case_id=case_dir.name, inject_time=inject_time,
                       evidence=evidence)


def discover_cases(dataset_dir: Path) -> list[Path]:
    """递归找出所有 case 目录（含 data.csv）。

    对应 RCAEval `main.py` 的 `glob("**/data.csv")`。
    """
    if not dataset_dir.exists():
        raise RcaEvalDataMissing(f"dataset directory not found: {dataset_dir}")
    return sorted(path.parent for path in dataset_dir.glob("**/data.csv"))


class EvidenceOnlyAdapters:
    """离线基准用的"数据源适配器"：直接从预置证据回放，不访问网络。

    为什么需要它：`run_investigation` 的采集层通过 adapters 调用数据源。
    离线基准没有活的 Prometheus/Loki，但我们**仍然要跑同一套调查流程** ——
    这就是蓝图 §10 "不伪装成 Grafana 数据源"的意思：不伪造一个 HTTP 服务，
    而是把**已经转换好的观测**按工具调用的形状返回。

    它返回的是 Prometheus/Loki 的**原生响应形状**，于是归一化、校验、
    引用完整性全部走与线上完全相同的代码路径。
    """

    def __init__(self, evidence: list[Evidence]) -> None:
        self.evidence = evidence

    def _by_service(self, service: str, evidence_type: EvidenceType) -> list[Evidence]:
        return [e for e in self.evidence
                if e.evidence_type is evidence_type and e.service_id == service]

    def query_service_error_rate(self, service: str, start, end) -> Any:
        matching = self._by_service(service, EvidenceType.METRIC)
        if not matching:
            raise ValueError(f"rcaeval: no metric evidence for {service}")
        series = [
            {"metric": item.normalized_data["series"][0]["metric"],
             "values": item.normalized_data["series"][0]["values"]}
            for item in matching
        ]
        return {"status": "success", "data": {"resultType": "matrix", "result": series}}

    def query_metric_series(self, service: str, metric: str, start, end) -> Any:
        return self.query_service_error_rate(service, start, end)

    def query_service_logs(self, service: str, start, end, filter=None) -> Any:
        raise ValueError(f"rcaeval: metrics-only dataset has no logs for {service}")

    def query_service_traces(self, service: str, start, end, min_duration_ms=None) -> Any:
        raise ValueError(f"rcaeval: metrics-only dataset has no traces for {service}")


def fault_type_for(metric: str) -> str:
    """RCAEval 的 metric 标签 → TraceOps 的 fault_type（弱映射，见模块 docstring）。

    未知 metric 返回 availability_degradation 而不是抛出：适配器的职责是
    尽量产出可诊断的观测，而不是替外部数据集校验词汇。
    """
    return METRIC_HINTS.get(metric, "availability_degradation")
