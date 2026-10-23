"""人的确认如何进记忆。

自动记录只能说明"系统这一次这样断言"，说明不了"人对过、是对的"。
两者的差别在复盘时是致命的：一个没人确认过的先验无法区分
"系统猜对了"与"系统猜错了但碰巧排在前面"。
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from traceops.cli.memory import main as memory_main
from traceops.runtime.incident_memory import IncidentMemory


class TestConfirmedPriors:
    def test_an_accepted_conclusion_is_both_a_hit_and_verified(self):
        memory = IncidentMemory()
        prior = memory.record_confirmation(service="order-service",
                                           fault_type="pool_exhaustion", accepted=True)

        assert prior.hits == 1
        assert prior.confirmed == 1
        assert prior.verified is True
        assert prior.corroborated is True
        memory.close()

    def test_a_rejected_conclusion_is_a_refutation_not_a_confirmation(self):
        memory = IncidentMemory()
        prior = memory.record_confirmation(service="order-service",
                                           fault_type="latency_degradation", accepted=False)

        assert prior.refuted == 1
        assert prior.confirmed == 0
        assert prior.verified is False
        assert prior.corroborated is False
        memory.close()

    def test_a_conclusion_cannot_be_both_refuted_and_confirmed(self):
        memory = IncidentMemory()
        with pytest.raises(ValueError, match="cannot also be confirmed"):
            memory.record(service="order-service", fault_type="pool_exhaustion",
                          refuted=True, confirmed=True)
        memory.close()

    def test_confirmation_adds_to_the_aggregate_when_no_component_is_given(self):
        memory = IncidentMemory()
        memory.record_confirmation(service="order-service", fault_type="pool_exhaustion",
                                   component="redis-connection-pool", accepted=True)
        memory.record_confirmation(service="order-service", fault_type="pool_exhaustion",
                                   component="mysql-connection-pool", accepted=True)

        aggregate = memory.recall(service="order-service", fault_type="pool_exhaustion")
        assert aggregate.confirmed == 2
        assert aggregate.hits == 2
        memory.close()

    def test_a_confirmed_prior_survives_reopening_the_database(self, tmp_path):
        path = tmp_path / "memory.db"
        first = IncidentMemory(path=path)
        first.record_confirmation(service="payment-service", fault_type="dependency_timeout",
                                 accepted=True)
        first.close()

        second = IncidentMemory(path=path)
        prior = second.recall(service="payment-service", fault_type="dependency_timeout")
        assert prior is not None and prior.confirmed == 1
        second.close()


class TestUpgradingAnOlderDatabase:
    def test_a_table_without_the_column_gains_it(self, tmp_path):
        """老库缺列时升级后必须能读，而不是 `no such column`。"""
        path = tmp_path / "old.db"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE memories (service TEXT NOT NULL, fault_type TEXT NOT NULL, "
            "component TEXT NOT NULL DEFAULT '', hits INTEGER NOT NULL DEFAULT 0, "
            "refuted INTEGER NOT NULL DEFAULT 0, last_seen TEXT NOT NULL, "
            "PRIMARY KEY (service, fault_type, component))")
        connection.execute(
            "INSERT INTO memories (service, fault_type, component, hits, refuted, last_seen) "
            "VALUES ('order-service', 'pool_exhaustion', '', 3, 0, '2026-01-01T00:00:00+00:00')")
        connection.commit()
        connection.close()

        memory = IncidentMemory(path=path)
        prior = memory.recall(service="order-service", fault_type="pool_exhaustion")
        assert prior is not None
        assert prior.hits == 3
        assert prior.confirmed == 0
        memory.close()


class TestTheOpsCommand:
    def test_confirming_from_an_export_applies_every_valid_row(self, tmp_path, capsys):
        export = tmp_path / "confirmations.json"
        export.write_text(json.dumps({"confirmations": [
            {"service": "order-service", "fault_type": "pool_exhaustion", "accepted": True},
            {"service": "order-service", "fault_type": "latency_degradation", "accepted": False},
            {"nonsense": True},
        ]}))
        db = tmp_path / "memory.db"

        code = memory_main(["confirm", "--json", str(export), "--memory-db", str(db)])
        assert code == 0
        printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert printed == {"applied": 2, "skipped": 1}

        memory = IncidentMemory(path=db)
        assert memory.recall(service="order-service", fault_type="pool_exhaustion").confirmed == 1
        assert memory.recall(service="order-service", fault_type="latency_degradation").refuted == 1
        memory.close()

    def test_the_db_flag_may_follow_the_subcommand(self, tmp_path):
        """`memory show --memory-db x` 是人的自然写法，不该报"无法识别的参数"。"""
        db = tmp_path / "memory.db"
        memory = IncidentMemory(path=db)
        memory.record(service="order-service", fault_type="pool_exhaustion")
        memory.close()

        assert memory_main(["show", "--memory-db", str(db)]) == 0

    def test_a_missing_file_is_an_error_not_a_silent_success(self, tmp_path, capsys):
        code = memory_main(["confirm", "--json", str(tmp_path / "nope.json"),
                            "--memory-db", str(tmp_path / "memory.db")])
        assert code == 2
        assert "no such file" in capsys.readouterr().err

    def test_invalid_json_is_reported(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        code = memory_main(["confirm", "--json", str(bad), "--memory-db", str(tmp_path / "m.db")])
        assert code == 2
        assert "not valid JSON" in capsys.readouterr().err

    def test_show_says_so_when_there_is_nothing(self, tmp_path, capsys):
        assert memory_main(["show", "--memory-db", str(tmp_path / "empty.db")]) == 0
        assert "no priors recorded" in capsys.readouterr().out
