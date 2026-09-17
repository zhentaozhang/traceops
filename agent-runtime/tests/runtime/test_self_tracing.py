"""自观测：TraceOps 自身也要产生 trace，否则它诊断别人时自己是黑盒。

## 为什么这不是"锦上添花"（PRD "可观测 | TraceOps 自身接 OTel ⬜"）

一个诊断平台如果自己不可观测，会有一个很难自查的盲区：

- 调查为什么慢？没有 span，只能靠日志猜
- 哪个节点耗时最多？只能看总时长
- Tool Gateway 的调用延迟分布？无数据

更重要的**双向验证**：TraceOps 已经在消费别人的 OTLP trace。
如果它自己产生的 trace 不能被同一条链路（Tempo/Collector）接受并查回，
那说明它对 OTLP 的理解有缺口 —— 那个缺口同样会影响它解析别人的数据。

## 约束：不引入 OTel SDK

`pyproject.toml` 刻意只依赖 stdlib + pydantic + jsonschema。
OTLP/HTTP-JSON 是一个**普通 JSON POST**，标准库足够 ——
项目已经用它往 Tempo push 过 trace（`scripts/e2e_live_stack.py`）。

## 本模块提供

- `Span` / `TraceRecorder`：构造 OTLP JSON（resourceSpans/scopeSpans/spans）
- 上下文管理器语义：`with recorder.span("collect"): ...` 自动记录起止与状态
- **失败不阻断业务**：上报失败只记 debug，绝不能让观测把调查搞挂
"""
from __future__ import annotations

import json

import pytest

from traceops.runtime.self_tracing import Span, SpanStatus, TraceRecorder


class TestSpanShape:
    def test_span_has_otlp_fields(self):
        span = Span(name="collect", start_unix_nano=1000, end_unix_nano=2000,
                    status=SpanStatus.OK)
        payload = span.to_otlp()
        assert payload["name"] == "collect"
        assert payload["startTimeUnixNano"] == "1000"
        assert payload["endTimeUnixNano"] == "2000"
        assert payload["status"]["code"] == "STATUS_CODE_OK"

    def test_error_status(self):
        span = Span(name="collect", start_unix_nano=1, end_unix_nano=2,
                    status=SpanStatus.ERROR)
        assert span.to_otlp()["status"]["code"] == "STATUS_CODE_ERROR"

    def test_ids_are_hex_not_base64(self):
        """实测教训：Tempo 2.5 的 OTLP 端点拒绝 base64 编码的 id。

        虽然 OTLP 规范写的是 base64，但**实测**只接受十六进制。
        这条约束已经在生产代码里踩过一次，自观测不能重蹈覆辙。
        """
        span = Span(name="collect", start_unix_nano=1, end_unix_nano=2,
                    status=SpanStatus.OK)
        payload = span.to_otlp()
        assert len(payload["traceId"]) == 32, payload["traceId"]
        assert len(payload["spanId"]) == 16, payload["spanId"]
        int(payload["traceId"], 16)  # 必须是合法 hex
        int(payload["spanId"], 16)

    def test_custom_attributes_are_carried(self):
        """span 级属性（如 evidence_count）要能带上，否则无从排查细节。"""
        span = Span(name="collect", start_unix_nano=1, end_unix_nano=2,
                    status=SpanStatus.OK, attributes={"evidence_count": 3})
        payload = span.to_otlp()
        keys = {a["key"] for a in payload["attributes"]}
        assert "evidence_count" in keys

    def test_service_name_lives_on_resource_not_span(self):
        """service.name 属于 resource 层（整个服务共用），不该每个 span 重复一遍。"""
        recorder = TraceRecorder(service_name="traceops-agent")
        with recorder.span("collect"):
            pass
        payload = recorder.to_otlp_payload()
        attrs = {a["key"] for a in payload["resourceSpans"][0]["resource"]["attributes"]}
        assert "service.name" in attrs
        span_attrs = {a["key"] for a in payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0].get("attributes", [])}
        assert "service.name" not in span_attrs


class TestRecorder:
    def test_records_a_span_around_a_block(self):
        recorder = TraceRecorder(service_name="traceops-agent")
        with recorder.span("collect"):
            pass
        assert len(recorder.spans()) == 1
        assert recorder.spans()[0].name == "collect"

    def test_failed_block_marks_error_status(self):
        recorder = TraceRecorder(service_name="traceops-agent")
        with pytest.raises(RuntimeError):
            with recorder.span("collect"):
                raise RuntimeError("datasource down")
        assert recorder.spans()[0].status is SpanStatus.ERROR

    def test_duration_is_recorded(self):
        recorder = TraceRecorder(service_name="traceops-agent")
        with recorder.span("collect"):
            pass
        span = recorder.spans()[0]
        assert int(span.end_unix_nano) > int(span.start_unix_nano)

    def test_export_builds_valid_otlp_payload(self):
        recorder = TraceRecorder(service_name="traceops-agent")
        with recorder.span("plan"):
            pass
        payload = recorder.to_otlp_payload()
        assert "resourceSpans" in payload
        resource = payload["resourceSpans"][0]["resource"]
        attrs = {a["key"]: a["value"]["stringValue"] for a in resource["attributes"]}
        assert attrs["service.name"] == "traceops-agent"

    def test_nested_spans(self):
        """嵌套是必须的：collect 内部的每个工具调用应当是它的子 span。"""
        recorder = TraceRecorder(service_name="traceops-agent")
        with recorder.span("collect"):
            with recorder.span("query_service_logs"):
                pass
        spans = recorder.spans()
        assert len(spans) == 2
        child = [s for s in spans if s.name == "query_service_logs"][0]
        parent = [s for s in spans if s.name == "collect"][0]
        assert child.parent_span_id == parent.span_id, "nested spans need parent linkage"


class TestExportNeverBreaksBusiness:
    def test_export_failure_is_non_fatal(self):
        """上报失败不能把调查搞挂 —— 观测是辅助，不能成为新的故障源。"""
        recorder = TraceRecorder(service_name="traceops-agent",
                                 endpoint="http://127.0.0.1:9/v1/traces")
        with recorder.span("collect"):
            pass
        # 端点不可达：export 必须返回 False 而不是抛异常
        assert recorder.export() is False

    def test_disabled_recorder_is_noop(self):
        recorder = TraceRecorder(service_name="traceops-agent", enabled=False)
        with recorder.span("collect"):
            pass
        assert recorder.spans() == []
        assert recorder.export() is True


def _time_kwargs():
    """统一的时间参数。

    注意不能用 `datetime.now()` 调两次：第二次会晚于第一次，
    触发 `observed_at before window_start` 的 Evidence 不变量。
    """
    from datetime import UTC, datetime as _dt
    now = _dt.now(UTC)
    return {"observed_at": now, "window": (now, now)}


class TestBidirectionalVerification:
    """最关键的一条：TraceOps 自己产生的 trace 必须能被**它自己**解析。

    这不是循环论证 —— 它检验的是"生产端与消费端对 OTLP 的理解一致"。
    TraceOps 已经在解析别人的 OTLP（`evidence/normalizer.py`），
    如果自家产出的 trace 过不了自家 Normalizer，说明理解有缺口，
    那个缺口同样会污染它对外部数据的解析结果。
    """

    def test_own_otlp_payload_parses_through_own_normalizer(self):
        from datetime import UTC, datetime

        from traceops.domain.models import EvidenceType
        from traceops.evidence.normalizer import normalize
        from traceops.runtime.self_tracing import TraceRecorder

        recorder = TraceRecorder(service_name="traceops-agent")
        with recorder.span("collect", evidence_count=3):
            with recorder.span("query_service_logs"):
                pass

        # 生产端产出的 OTLP 直接被消费端解析
        evidence = normalize(
            evidence_type=EvidenceType.TRACE,
            source_id="self",
            incident_id="inc-self",
            run_id="run-self",
            service_id="traceops-agent",
            query_template_id="qt-self",
            query_params={},
            raw_payload=recorder.to_otlp_payload(),
            **_time_kwargs(),
        )

        assert evidence.normalized_data["total_records"] == 2, (
            "our own OTLP must survive our own normalizer")
        names = {s["operation_name"] for s in evidence.normalized_data["spans"]}
        assert names == {"collect", "query_service_logs"}

    def test_own_spans_preserve_parent_child_relationship(self):
        """父子关系必须能往返 —— 否则耗时无法归因到具体工具调用。"""
        from datetime import UTC, datetime

        from traceops.domain.models import EvidenceType
        from traceops.evidence.normalizer import normalize
        from traceops.runtime.self_tracing import TraceRecorder

        recorder = TraceRecorder(service_name="traceops-agent")
        with recorder.span("collect"):
            with recorder.span("query_service_logs"):
                pass

        evidence = normalize(
            evidence_type=EvidenceType.TRACE, source_id="self",
            incident_id="inc", run_id="run", service_id="traceops-agent",
            query_template_id="qt", query_params={},
            raw_payload=recorder.to_otlp_payload(),
            **_time_kwargs(),
        )
        # spans 里应保留 trace_id 关联（同一 trace）
        trace_ids = {s["trace_id"] for s in evidence.normalized_data["spans"]}
        assert len(trace_ids) == 1, "nested spans belong to one trace"
