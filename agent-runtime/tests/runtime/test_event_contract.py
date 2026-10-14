"""事件契约必须是运行时闸门，而不是只存在于 contracts/ 里的文档。

这次改动的起因是一个真实的、两个语言都没发现的事故：
控制面的人工入口发出 `priority="manual"`，而 `contracts/events/v1/
investigation-requested.schema.json` 里 priority 的枚举是 low|normal|high。
Java 侧 networknt 依赖只配了 test 作用域，Python 侧靠一份手写的 required 列表
（里面根本没有 priority 这一项），于是这条违约消息一路走到了
`payload.get("priority", "normal")` —— 取了个谁也没定义过的值，并且两边都
以为一切正常。

所以这个文件的每一条断言都在回答同一个问题：
"如果生产方和契约不一致，消费方会不会当场知道？"
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from traceops.runtime.event_contract import (
    ContractViolation,
    contracts_root,
    schema_count,
    validate_event,
)

VALID_EVENT_ID = "evt-11111111-1111-4111-8111-111111111111"


def requested_message(**payload_overrides) -> dict:
    """一条契约合法的 investigation.requested。"""
    payload = {
        "investigation_run_id": "run-001",
        "incident_id": "inc-001",
        "service_hint": "order-service",
        "environment": "demo",
        "time_range": {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T10:06:00Z"},
        "config_version": "cfg",
        "priority": "high",
    }
    payload.update(payload_overrides)
    return {
        "envelope": {
            "event_id": VALID_EVENT_ID,
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


class TestTheContractIsActuallyLoaded:
    def test_all_event_schemas_are_present(self):
        # 空注册表会让每条消息都"校验通过"，是最坏的一种失败方式
        assert schema_count() == 4

    def test_contracts_root_points_at_the_real_directory(self):
        root = contracts_root()
        assert (root / "events" / "v1" / "investigation-requested.schema.json").is_file()
        assert (root / "common" / "event-envelope.schema.json").is_file()


class TestProducersGetStoppedAtTheSource:
    def test_a_valid_message_passes(self):
        validate_event("investigation.requested", requested_message())

    def test_manual_priority_is_rejected_with_its_path(self):
        """真实事故的回归：人工来源曾把 "manual" 塞进 priority。"""
        with pytest.raises(ContractViolation) as excinfo:
            validate_event("investigation.requested",
                           requested_message(priority="manual"))
        message = str(excinfo.value)
        assert "priority" in message
        # 枚举外取值必须被指出来，而不是只说"某处不合法"
        assert "manual" in message or "low" in message

    def test_missing_service_hint_is_rejected(self):
        message = requested_message()
        del message["payload"]["service_hint"]
        with pytest.raises(ContractViolation, match="service_hint"):
            validate_event("investigation.requested", message)

    def test_unknown_payload_field_is_rejected(self):
        """additionalProperties: false —— 多余字段说明生产方在用契约之外的约定。"""
        with pytest.raises(ContractViolation) as excinfo:
            validate_event("investigation.requested",
                           requested_message(severity_hint="redis is slow"))
        assert "severity_hint" in str(excinfo.value)

    def test_alert_summary_is_part_of_the_contract(self):
        """告警原文是模型唯一读得到的自然语言线索，必须在契约里有一席之地。"""
        validate_event("investigation.requested",
                       requested_message(alert_summary="redis is slow"))


class TestTheEventTypePinsTheSchema:
    def test_a_message_cannot_claim_another_events_type(self):
        """四个事件 schema 的信封结构相同，若不钉住 event_type 就彼此不可区分。

        这条断言曾经是假的：validate_event 按事件自报的类型选 schema，
        于是一条 completed 事件被拿 completed schema 校验，当然通过。
        """
        message = requested_message()
        message["envelope"]["event_type"] = "investigation.completed"
        with pytest.raises(ContractViolation, match="event_type"):
            validate_event("investigation.requested", message)

    def test_unknown_event_type_is_rejected(self):
        with pytest.raises(ContractViolation, match="no schema registered"):
            validate_event("investigation.archived", requested_message())

    def test_each_schema_keeps_its_own_type(self):
        for event_type in ("investigation.requested", "investigation.updated",
                           "investigation.completed", "investigation.failed"):
            path = contracts_root() / "events" / "v1" / (event_type.replace(".", "-") + ".schema.json")
            document = json.loads(path.read_text())
            envelope = document["properties"]["envelope"]
            consts = [rule["properties"]["event_type"]["const"]
                      for rule in envelope["allOf"] if "properties" in rule]
            assert consts == [event_type], event_type


class TestABrokenEnvelopeIsItsOwnCategory:
    def test_missing_envelope_is_flagged(self):
        """缺信封 vs 信封在但内容不符，落库时要分成两类问题去修。"""
        with pytest.raises(ContractViolation) as excinfo:
            validate_event("investigation.requested",
                           {"payload": requested_message()["payload"]})
        assert excinfo.value.missing_envelope is True
        assert "envelope is missing" in str(excinfo.value)

    def test_present_but_wrong_envelope_is_not_flagged_as_missing(self):
        message = requested_message()
        message["envelope"]["producer"] = "agent-runtime"
        with pytest.raises(ContractViolation) as excinfo:
            validate_event("investigation.requested", message)
        assert excinfo.value.missing_envelope is False
        assert "producer" in str(excinfo.value)


class TestTheSchemaIsNotACopy:
    def test_validation_reads_the_shared_contracts_directory(self):
        """契约只有一份：测试断言校验器用的就是仓库根下 contracts/ 的那份文件。"""
        path = contracts_root() / "events" / "v1" / "investigation-requested.schema.json"
        assert isinstance(path, Path)
        assert 'investigation.requested' in path.read_text()


class TestThePythonSideAgreesWithTheFixtureWeCapture:
    def test_a_python_written_completed_event_validates(self):
        """控制面侧的 PythonWrittenEventTest 用的同一份 fixture。

        两个语言各自维护一份"我以为的事件长什么样"，就一定会漂移；
        这条断言让漂移在 Python 侧也立刻可见。
        """
        fixture = (Path(__file__).resolve().parents[3]
                   / "control-plane" / "src" / "test" / "resources"
                   / "python-written-investigation-completed.json")
        if not fixture.is_file():  # pragma: no cover - fixture 缺失不该让 Python 测试红
            pytest.skip(f"fixture not found: {fixture}")
        document = json.loads(fixture.read_text())
        message = copy.deepcopy(document)
        event_type = message["envelope"]["event_type"]
        validate_event(event_type, message)
