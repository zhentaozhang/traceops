"""demo 是产品的门面：一条命令、零外部依赖、产出一份能读的诊断。

它不能因为"反正是 demo"就造假：数据源是合成的（这一点必须写在输出里），
但编排、证据、排序、报告、渲染全部走生产路径。
"""
from __future__ import annotations

import json

from traceops.cli.demo import main


def test_demo_runs_and_prints_the_four_sections(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "合成数据" in out or "数据源为合成数据" in out
    assert "结论" in out
    assert "为什么信" in out
    assert "下一步（建议，不是结论）" in out
    assert "不确定的地方" in out
    assert "成本" in out


def test_demo_reaches_a_supported_diagnosis(capsys):
    main([])
    out = capsys.readouterr().out
    assert "已确认" in out
    assert "redis" in out


def test_demo_writes_the_raw_report_when_asked(tmp_path, capsys):
    target = tmp_path / "nested" / "report.json"
    assert main(["--json", str(target)]) == 0
    capsys.readouterr()
    payload = json.loads(target.read_text(encoding="utf-8"))
    # 报告的形状是产品契约的一部分：关键字段缺一个，下游就少一个判断依据
    for key in (
        "diagnosis_status",
        "summary",
        "hypotheses",
        "evidence",
        "hypothesis_evidence",
        "missing_data",
        "no_data",
        "degraded_reads",
        "run_snapshot",
    ):
        assert key in payload
    assert payload["diagnosis_status"] == "SUPPORTED"
    assert payload["evidence"], "demo 必须真的取到证据"


def test_demo_verbose_shows_the_raw_summary(capsys):
    assert main(["--verbose"]) == 0
    out = capsys.readouterr().out
    assert "原始 summary" in out


def test_demo_declares_the_synthetic_scope(capsys):
    """把"数据是假的"写成第一行，而不是藏在注释里。"""
    main([])
    out = capsys.readouterr().out
    first_lines = "\n".join(out.splitlines()[:2])
    assert "合成" in first_lines
