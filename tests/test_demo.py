"""End-to-end: the scripted demo runs all 8 steps and finds the two injected faults."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from tbfmon.cli import app

runner = CliRunner()


def test_demo_end_to_end(tmp_path):
    target = tmp_path / "demo"
    res = runner.invoke(app, ["demo", "--fast", "--fans", "16", "--dir", str(target)])
    assert res.exit_code == 0, res.output
    for k in range(1, 9):
        assert f"Step {k}/8" in res.output
    assert "SIMULATED" in res.output
    assert "caught 2/2 developing faults" in res.output
    assert "Traceback" not in res.output
    # the demo leaves a normal workspace behind that the other commands can use
    alerts = json.loads(runner.invoke(app, ["--home", str(target), "alerts", "list", "--json"]).stdout)["alerts"]
    assert {a["severity"] for a in alerts} & {"ALARM", "CRITICAL"}
    # re-running recreates the marked demo folder
    assert runner.invoke(app, ["demo", "--fast", "--fans", "16", "--dir", str(target)]).exit_code == 0


def test_demo_refuses_foreign_folder(tmp_path):
    (tmp_path / "precious.txt").write_text("do not delete")
    res = runner.invoke(app, ["demo", "--fast", "--dir", str(tmp_path)])
    assert res.exit_code == 3 and (tmp_path / "precious.txt").exists()
