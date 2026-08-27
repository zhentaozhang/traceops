"""OTLP 格式 Trace 归一化测试（真实 Tempo 数据的实际形状）。

## 这是一个被真实环境抓出来的缺陷

现有 `TraceNormalizer` 期望 Jaeger 风格：
    batches[].spans[].traceID / operationName / startTime / duration

但**真实 Tempo `/api/traces/{id}` 返回的是 OTLP 标准格式**：
    batches[].scopeSpans[].spans[].traceId（base64）/ name / startTimeUnixNano / endTimeUnixNano

结果：所有真实 trace 证据都会被判为"空 trace"而丢弃。假传输层永远发现不了
这个问题，因为假数据是按 Normalizer 的期望造的。

## 必须同时支持两种

Jaeger 格式在测试/旧数据里仍存在，OTLP 是生产主流。两者都要能解析。
"""
from __future__ import annotations

import base64
import pytest

from traceops.evidence.normalizer import TraceNormalizer


def otlp_trace(*, trace_id_hex: str = "4bf92f3577b34da6a3ce929d0e0e4736",
               span_hex: str = "00f067aa0ba902b7",
               service: str = "order-service",
               name: str = "redis.command",
               start_ns: int = 1793414383835237120,
               end_ns: int = 1793414385835237120,
               error: bool = True,
               extra_tags: dict | None = None) -> dict:
    """真实 Tempo /api/traces/{id} 的返回形状（OTLP）。"""
    attributes = [{"key": "service.name", "value": {"stringValue": service}}]
    for key, value in (extra_tags or {}).items():
        attributes.append({"key": key, "value": {"stringValue": str(value)}})
    span = {
        "traceId": base64.b64encode(bytes.fromhex(trace_id_hex)).decode(),
        "spanId": base64.b64encode(bytes.fromhex(span_hex)).decode(),
        "name": name,
        "kind": "SPAN_KIND_CLIENT",
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
    }
    if error:
        span["status"] = {"code": "STATUS_CODE_ERROR", "message": "connection pool exhausted"}
    return {"batches": [{
        "resource": {"attributes": attributes},
        "scopeSpans": [{"scope": {"name": "tracer"}, "spans": [span]}],
    }]}


class TestOtlpTraceParsing:
    def test_otlp_batch_is_parsed(self):
        """核心：OTLP 形状必须产出 spans，不能判为空。"""
        out = TraceNormalizer().normalize(otlp_trace(), max_records=500)
        assert out["kind"] == "trace"
        assert len(out["spans"]) == 1

    def test_base64_trace_id_decoded_to_hex(self):
        """OTLP 用 base64 编码 id；下游按十六进制做关联（Loki 日志里也是十六进制）。"""
        out = TraceNormalizer().normalize(otlp_trace(), max_records=500)
        span = out["spans"][0]
        assert span["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert span["span_id"] == "00f067aa0ba902b7"

    def test_resource_service_name_extracted_from_attributes(self):
        """OTLP 把服务名放在 resource.attributes 数组里，不是 resource["service.name"]。"""
        out = TraceNormalizer().normalize(otlp_trace(), max_records=500)
        assert out["spans"][0]["service"] == "order-service"

    def test_nano_timestamps_converted_to_duration(self):
        """OTLP 只给 start/end 纳秒；duration 必须由差值算出（Jaeger 是直接给 duration）。"""
        out = TraceNormalizer().normalize(otlp_trace(), max_records=500)
        span = out["spans"][0]
        # end - start = 2s = 2_000_000 us
        assert span["duration_us"] == 2_000_000

    def test_status_code_error_marks_span_as_error(self):
        out = TraceNormalizer().normalize(otlp_trace(), max_records=500)
        assert out["spans"][0]["error"] is True

    def test_ok_status_marks_span_as_not_error(self):
        out = TraceNormalizer().normalize(otlp_trace(error=False), max_records=500)
        assert out["spans"][0]["error"] is False

    def test_operation_name_mapped_from_otlp_name(self):
        out = TraceNormalizer().normalize(otlp_trace(), max_records=500)
        assert out["spans"][0]["operation_name"] == "redis.command"

    def test_extra_resource_attributes_become_tags(self):
        out = TraceNormalizer().normalize(
            otlp_trace(extra_tags={"host.name": "node-1"}), max_records=500)
        assert out["spans"][0]["tags"].get("host.name") == "node-1"

    def test_multiple_scopes_and_spans_all_collected(self):
        payload = otlp_trace()
        # 第二个 scope 里再放一个 span
        payload["batches"][0]["scopeSpans"].append({
            "scope": {"name": "other"},
            "spans": [{
                "traceId": base64.b64encode(bytes.fromhex("a" * 32)).decode(),
                "spanId": base64.b64encode(bytes.fromhex("b" * 16)).decode(),
                "name": "http.downstream",
                "startTimeUnixNano": "1793414383000000000",
                "endTimeUnixNano": "1793414386000000000",
            }],
        })
        out = TraceNormalizer().normalize(payload, max_records=500)
        assert len(out["spans"]) == 2


class TestJaegerStillWorks:
    """Jaeger 格式不能被 OTLP 支持破坏 —— 旧数据与测试仍用它。"""

    def test_jaeger_shape_still_parsed(self):
        jaeger = {"batches": [{
            "resource": {"service.name": "order-service"},
            "spans": [{
                "traceID": "4bf92f3577b34da6",
                "spanID": "00f067aa0ba902b7",
                "operationName": "redis.command",
                "startTime": "2026-10-30T02:00:00Z",
                "duration": 2000,
                "tags": [{"key": "error", "value": True}],
            }],
        }]}
        out = TraceNormalizer().normalize(jaeger, max_records=500)
        assert len(out["spans"]) == 1
        assert out["spans"][0]["trace_id"] == "4bf92f3577b34da6"
        assert out["spans"][0]["error"] is True


class TestRealTempoRegression:
    """用真实 Tempo 抓到的实际字节做回归，防止形状再次漂移。"""

    def test_actual_tempo_response_shape(self):
        real = {
            "batches": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "order-service"}}]},
                "scopeSpans": [{
                    "scope": {"name": "tracer"},
                    "spans": [{
                        "traceId": "S/kvNXezTaajzpKdDg5HNg==",
                        "spanId": "APBnqgupArc=",
                        "name": "redis.command",
                        "kind": "SPAN_KIND_CLIENT",
                        "startTimeUnixNano": "1793414383835237120",
                        "endTimeUnixNano": "1793414385835237120",
                        "status": {"message": "connection pool exhausted",
                                   "code": "STATUS_CODE_ERROR"},
                    }],
                }],
            }],
        }
        out = TraceNormalizer().normalize(real, max_records=500)
        span = out["spans"][0]
        assert span["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert span["service"] == "order-service"
        assert span["duration_us"] == 2_000_000
        assert span["error"] is True
