"""查询模板测试：模板化是为了防注入、可审计、可复现。"""
from __future__ import annotations

import pytest

from traceops.evidence.templates import (
    QueryTemplate,
    QueryTemplateError,
    TemplateRegistry,
    default_registry,
)
from traceops.domain.models import EvidenceType

START = "2026-10-30T10:00:00Z"
END = "2026-10-30T10:06:00Z"


def params(**overrides):
    base = {"service": "order-service", "start": START, "end": END}
    base.update(overrides)
    return base


class TestBuiltinTemplates:
    def test_all_four_templates_registered(self):
        registry = default_registry()
        ids = registry.list_ids()
        assert "qt-error-rate-v1" in ids
        assert "qt-metric-series-v1" in ids
        assert "qt-error-logs-v1" in ids
        assert "qt-slow-traces-v1" in ids

    def test_error_rate_renders_promql(self):
        query = default_registry().build("qt-error-rate-v1", params())
        assert 'service="order-service"' in query
        assert 'status=~"5.."' in query
        assert "clamp_min" in query

    def test_metric_series_renders_selector(self):
        query = default_registry().build("qt-metric-series-v1",
                                         params(metric="redis_connected_clients"))
        assert query == 'redis_connected_clients{service="order-service"}'

    def test_logs_without_filter(self):
        query = default_registry().build("qt-error-logs-v1", params())
        assert query == '{service="order-service",level="error"}'

    def test_logs_with_filter(self):
        query = default_registry().build("qt-error-logs-v1", params(filter="timeout"))
        assert query == '{service="order-service",level="error"} |= "timeout"'

    def test_traces_with_duration(self):
        query = default_registry().build("qt-slow-traces-v1",
                                         params(min_duration_ms=500))
        assert 'service.name="order-service"' in query
        assert "duration > 500ms" in query

    def test_traces_without_duration(self):
        query = default_registry().build("qt-slow-traces-v1", params())
        assert query == '{service.name="order-service"}'


class TestInjectionDefences:
    def test_service_name_with_quote_rejected(self):
        """服务名里带引号可以闭合 selector —— 必须拒绝。"""
        with pytest.raises(QueryTemplateError, match="invalid parameters"):
            default_registry().build("qt-error-logs-v1",
                                     params(service='x"} |= ""'))

    def test_service_name_with_backslash_rejected(self):
        with pytest.raises(QueryTemplateError):
            default_registry().build("qt-error-logs-v1", params(service="a\\b"))

    def test_service_name_with_regex_metachars_rejected(self):
        with pytest.raises(QueryTemplateError):
            default_registry().build("qt-error-logs-v1", params(service=".*"))

    def test_filter_with_quote_rejected(self):
        """过滤词带引号能闭合 LogQL 字符串 —— 必须拒绝。"""
        with pytest.raises(QueryTemplateError):
            default_registry().build("qt-error-logs-v1",
                                     params(filter='x" or {job=~".*"'))

    def test_filter_with_backslash_rejected(self):
        with pytest.raises(QueryTemplateError):
            default_registry().build("qt-error-logs-v1", params(filter="a\\b"))

    def test_metric_name_must_be_identifier(self):
        with pytest.raises(QueryTemplateError):
            default_registry().build("qt-metric-series-v1",
                                     params(metric='x{foo="1"}'))

    def test_extra_parameter_rejected(self):
        """多传参数不允许：模板只接受自己声明的输入。"""
        with pytest.raises(QueryTemplateError):
            default_registry().build("qt-error-rate-v1", params(extra="boom"))

    def test_missing_required_parameter_rejected(self):
        with pytest.raises(QueryTemplateError):
            default_registry().build("qt-error-rate-v1", {"start": START, "end": END})


class TestRegistryBehaviour:
    def test_unknown_template_raises(self):
        with pytest.raises(QueryTemplateError, match="not registered"):
            default_registry().build("qt-make-it-up", params())

    def test_duplicate_registration_rejected(self):
        registry = TemplateRegistry()
        template = QueryTemplate(
            template_id="dup", description="d", evidence_type=EvidenceType.METRIC,
            parameter_schema={"type": "object"}, render=lambda p: "x")
        registry.register(template)
        with pytest.raises(QueryTemplateError, match="already registered"):
            registry.register(template)

    def test_empty_registry_rejects_everything(self):
        registry = TemplateRegistry()
        assert registry.list_ids() == []
        with pytest.raises(QueryTemplateError):
            registry.build("qt-error-rate-v1", params())

    def test_render_is_pure(self):
        """同一参数渲染两次必须完全相同 —— 否则无法复现。"""
        registry = default_registry()
        first = registry.build("qt-error-logs-v1", params(filter="timeout"))
        second = registry.build("qt-error-logs-v1", params(filter="timeout"))
        assert first == second
