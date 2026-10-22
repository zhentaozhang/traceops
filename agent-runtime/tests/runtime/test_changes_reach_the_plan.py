"""变更清单必须从消息一路走到计划项。

## 被替换的缺陷

契约里只有一个 `config_change`（change_kind 固定为"配置"、只有一条，
且强制 `component` 必填）。真实事故里"变了什么"往往是好几件事：
一次发版同时改配置、同时扩缩容、同时开了一个开关。只收单条会逼调用方
挑一条，而被丢掉的那条可能才是原因 —— 而且丢的时候没人知道丢了。

## 为什么 `change_kind` 要显式区分

`config` 与 `deployment` 在证据层都是 EVENT，但进报告时"发布了一个新版本"
和"改了一个配置项"要分开说：前者指向代码，后者指向参数。模板 id 也据此分开
（`qt-deployment-v1` / `qt-config-change-v1`），否则审计里两类变更混成一片。

## 为什么坏记录只跳过一条

一条 `changed_at` 拼错的变更不该让整趟调查失败：其余变更仍是有效证据。
但也不能静默 —— 跳过时不再进计划，报告里就查不到它，这一点写在
`_change_items` 的注释里，是已知的边界。
"""
from __future__ import annotations

from datetime import UTC, datetime

from traceops.orchestration.graph import InvestigationRequest, _change_items
from traceops.runtime.worker import derive_request_from_event

EVENT_ID = "evt-22222222-2222-4222-8222-222222222222"


def _message(payload_extra: dict) -> dict:
    payload = {
        "investigation_run_id": "run-001",
        "incident_id": "inc-001",
        "service_hint": "order-service",
        "environment": "demo",
        "time_range": {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:06:00Z"},
        "config_version": "cfg",
        "priority": "normal",
    }
    payload.update(payload_extra)
    return {
        "envelope": {
            "event_id": EVENT_ID,
            "event_type": "investigation.requested",
            "schema_version": "1.0",
            "occurred_at": "2026-10-30T10:06:00Z",
            "correlation_id": "inc-001",
            "causation_id": None,
            "aggregate_id": "run-001",
            "aggregate_version": 1,
            "producer": "control-plane",
        },
        "payload": payload,
    }


CHANGES = [
    {"changed_at": "2026-10-30T09:58:00Z", "change_kind": "deployment",
     "service": "checkout-service", "revision": "r42", "summary": "release r42"},
    {"changed_at": "2026-10-30T09:59:30Z", "change_kind": "feature_flag",
     "service": "checkout-service", "summary": "enabled new pricing path"},
]


class TestTheChangesSurviveDerivation:
    def test_all_changes_are_kept(self):
        request = derive_request_from_event(_message({"changes": CHANGES}))

        assert [c["change_kind"] for c in request.changes] == ["deployment", "feature_flag"]

    def test_a_message_without_changes_yields_none_rather_than_an_empty_list(self):
        # None = 消息没带变更信息；[] = 带了且确认"没有变更"。
        # 混同会让"没人告诉我们"变成"确认无变更"。
        request = derive_request_from_event(_message({}))

        assert request.changes is None


class TestChangesBecomePlanItems:
    def _request(self, **extra) -> InvestigationRequest:
        base = dict(
            investigation_run_id="run-001",
            incident_id="inc-001",
            environment="demo",
            service_hint="order-service",
            fault_start=datetime(2026, 10, 30, 10, 6, tzinfo=UTC),
            window_start=datetime(2026, 10, 30, 10, 0, tzinfo=UTC),
            window_end=datetime(2026, 10, 30, 10, 6, tzinfo=UTC),
        )
        base.update(extra)
        return InvestigationRequest(**base)

    def test_deployments_and_config_changes_get_different_templates(self):
        items = _change_items(self._request(changes=CHANGES))

        assert [i["query_template_id"] for i in items] == [
            "qt-deployment-v1", "qt-config-change-v1"]

    def test_each_item_carries_the_change_as_its_payload(self):
        items = _change_items(self._request(changes=CHANGES))

        # payload 存在即"这条不是数据源查询，别去查它" —— `_collect_node` 靠这个判据
        assert items[0]["payload"]["revision"] == "r42"
        assert items[0]["service"] == "checkout-service"

    def test_a_single_config_change_still_works(self):
        items = _change_items(self._request(config_change={
            "changed_at": "2026-10-30T09:57:00Z",
            "component": "checkout-service",
            "summary": "bumped connection pool",
        }))

        assert [i["query_template_id"] for i in items] == ["qt-config-change-v1"]
        assert items[0]["payload"]["change_kind"] == "config"

    def test_a_malformed_change_does_not_kill_the_others(self):
        items = _change_items(self._request(changes=[
            {"changed_at": "not-a-date", "change_kind": "deployment"},
            CHANGES[1],
        ]))

        assert len(items) == 1
        assert items[0]["query_template_id"] == "qt-config-change-v1"
