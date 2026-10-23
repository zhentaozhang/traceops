"""遥测约定可声明。

模板把 `http_requests_total`、标签 `service`、`status=~"5.."` 写死在代码里过。
这些是某一套采集配置的产物，不是事实：名字对不上时查询返回空，而空结果在
下游被当成"阴性观测"（"查了，没有任何异常"），不是错误 —— 于是"你的集群
不符合我们的约定"被伪装成"你的系统很健康"。
"""
from __future__ import annotations

import json

import pytest

from traceops.evidence.templates import (
    QueryTemplateError,
    TelemetryConventions,
    default_registry,
    load_conventions,
    render_error_rate,
    render_logs,
    render_traces,
)

PARAMS = {
    "service": "checkout-service",
    "start": "2026-03-12T14:22:00+00:00",
    "end": "2026-03-12T14:37:00+00:00",
}


def _write(tmp_path, payload) -> str:
    path = tmp_path / "conventions.json"
    path.write_text(json.dumps(payload))
    return str(path)


class TestDefaultsAreTheDemoStack:
    def test_error_rate_query_is_unchanged(self):
        rendered = default_registry().build("qt-error-rate-v1", PARAMS)
        assert 'http_requests_total{service="checkout-service",status=~"5.."}' in rendered
        assert "clamp_min" in rendered

    def test_log_and_trace_selectors_are_unchanged(self):
        registry = default_registry()
        assert registry.build("qt-error-logs-v1", PARAMS) == '{service="checkout-service",level="error"}'
        assert registry.build(
            "qt-slow-traces-v1", {**PARAMS, "min_duration_ms": 500}
        ) == '{service.name="checkout-service"} && duration > 500ms'

    def test_metric_series_uses_the_declared_service_label(self):
        rendered = default_registry().build(
            "qt-metric-series-v1", {**PARAMS, "metric": "redis_connected_clients"}
        )
        assert rendered == 'redis_connected_clients{service="checkout-service"}'


class TestDeclaredConventionsChangeEveryQuery:
    def test_a_different_metric_and_label_reach_all_templates(self, tmp_path):
        path = _write(tmp_path, {
            "requests_total": "http_server_requests_count",
            "service_label": "app",
            "error_status_regex": "5[0-9][0-9]",
            "log_service_label": "app",
            "log_level_label": "severity",
            "log_error_value": "ERROR",
            "trace_service_label": "service.name",
        })
        registry = default_registry(load_conventions(path))

        error_rate = registry.build("qt-error-rate-v1", PARAMS)
        assert "http_server_requests_count" in error_rate
        assert 'app="checkout-service"' in error_rate
        assert 'status=~"5[0-9][0-9]"' in error_rate
        assert "http_requests_total" not in error_rate

        assert registry.build("qt-error-logs-v1", PARAMS) == '{app="checkout-service",severity="ERROR"}'
        assert registry.build(
            "qt-metric-series-v1", {**PARAMS, "metric": "redis_connected_clients"}
        ) == 'redis_connected_clients{app="checkout-service"}'

    def test_a_partial_file_keeps_the_other_defaults(self, tmp_path):
        conventions = load_conventions(_write(tmp_path, {"requests_total": "http_server_requests_count"}))
        assert conventions.requests_total == "http_server_requests_count"
        assert conventions.service_label == "service"

    def test_the_discrimination_loop_uses_the_same_renderer(self):
        conventions = TelemetryConventions(requests_total="http_server_requests_count",
                                          service_label="app")
        assert render_error_rate("payment-service", conventions) == (
            'sum(rate(http_server_requests_count{app="payment-service",status=~"5.."}[1m]))'
            ' / clamp_min(sum(rate(http_server_requests_count{app="payment-service"}[1m])), 0.000001)'
        )


class TestBadDeclarationsAreRefused:
    def test_unknown_field_is_refused_and_names_the_known_ones(self, tmp_path):
        with pytest.raises(QueryTemplateError, match="unknown convention field"):
            load_conventions(_write(tmp_path, {"request_total": "x"}))

    def test_a_metric_name_that_is_not_a_metric_name_is_refused(self):
        with pytest.raises(QueryTemplateError, match="Prometheus metric name"):
            TelemetryConventions(requests_total='http{injected="x"}')

    def test_a_label_name_that_could_break_the_selector_is_refused(self):
        with pytest.raises(QueryTemplateError, match="label name"):
            TelemetryConventions(service_label='service"}[1m])) or up{')

    def test_an_invalid_status_regex_is_refused(self):
        with pytest.raises(QueryTemplateError, match="regular expression"):
            TelemetryConventions(error_status_regex="5..(")

    def test_a_blank_log_level_is_refused(self):
        with pytest.raises(QueryTemplateError, match="log_error_value"):
            TelemetryConventions(log_error_value="   ")

    def test_a_non_object_file_is_refused(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[1, 2]")
        with pytest.raises(QueryTemplateError, match="must hold an object"):
            load_conventions(str(path))

    def test_a_non_string_value_is_refused(self, tmp_path):
        with pytest.raises(QueryTemplateError, match="must be a string"):
            load_conventions(_write(tmp_path, {"requests_total": 42}))


class TestRenderersAcceptFilters:
    def test_a_filter_word_is_appended_to_the_log_selector(self):
        assert render_logs("api", TelemetryConventions(), filter_text="timeout") == (
            '{service="api",level="error"} |= "timeout"'
        )

    def test_traces_without_a_duration_do_not_add_a_condition(self):
        assert render_traces("api", TelemetryConventions()) == '{service.name="api"}'
