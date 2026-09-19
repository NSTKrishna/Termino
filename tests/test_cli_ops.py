"""analyze, fleet (health view) and explain through the CLI."""

from __future__ import annotations

import json
import sqlite3

from typer.testing import CliRunner

from tbfmon.cli import app

runner = CliRunner()


def call(home, *args):
    return runner.invoke(app, ["--home", str(home), *args])


def js(home, *args):
    res = call(home, *args, "--json")
    assert res.exit_code in (0, 1, 2), res.output
    return json.loads(res.stdout), res.exit_code


def test_analyze_latest_json_and_exit_code(cli_home):
    d, code = js(cli_home, "analyze", "--fan", "3", "--latest")
    assert d["fan_id"] == "TBF-003" and d["is_latest_run"]
    assert code == {"Healthy": 0, "Watch": 1, "Degraded": 1, "Critical": 2}[d["status"]] == d["exit_code"]
    statuses = {p["status"] for p in d["params"]}
    assert statuses <= {"OK", "WARN", "ALARM", "SUSPECT", "no limit", "not measured"}
    vib = next(p for p in d["params"] if p["parameter"] == "vib_de_rms_mms")
    assert vib["warn"] == 4.5 and vib["alarm"] == 7.1
    assert {"action", "by_date", "tasks", "needs_work_at_height"} <= set(d["recommendation"])


def test_analyze_past_run_is_point_in_time(cli_home):
    con = sqlite3.connect(cli_home / "tbfmon.db")
    run_id = con.execute("SELECT run_id FROM test_run WHERE fan_id='TBF-003' ORDER BY ts LIMIT 1 OFFSET 5").fetchone()[0]
    d, _ = js(cli_home, "analyze", "--fan", "TBF-003", "--run", str(run_id))
    assert d["run_id"] == run_id and not d["is_latest_run"]


def test_analyze_friendly_errors(cli_home):
    res = call(cli_home, "analyze", "--fan", "TBF-0O3", "--json")
    assert res.exit_code == 0 or res.exit_code in (1, 2)  # O-for-zero typo resolves to TBF-003
    res = call(cli_home, "analyze", "--fan", "XYZ-999", "--json")
    assert res.exit_code == 3 and json.loads(res.stdout)["error"].startswith("unknown fan")
    con = sqlite3.connect(cli_home / "tbfmon.db")
    other = con.execute("SELECT run_id FROM test_run WHERE fan_id='TBF-004' LIMIT 1").fetchone()[0]
    res = call(cli_home, "analyze", "--fan", "TBF-003", "--run", str(other), "--json")
    assert res.exit_code == 3 and "recent runs" in json.loads(res.stdout)["hint"]
    assert call(cli_home, "analyze", "--fan", "3", "--run", "1", "--latest").exit_code == 3
    assert call(cli_home, "analyze").exit_code == 3  # missing --fan is a usage error, not an alarm


def test_analyze_human_output(cli_home):
    res = call(cli_home, "analyze", "--fan", "TBF-003")
    assert res.exit_code in (0, 1, 2)
    assert "Health" in res.output and "Recommended action" in res.output and "deviation %" in res.output


def test_fleet_health_and_filters(cli_home):
    d, code = js(cli_home, "fleet")
    assert len(d["fans"]) == 16
    order = {"Critical": 0, "Degraded": 1, "Watch": 2, "Healthy": 3}
    ranks = [order[f["status"]] for f in d["fans"]]
    assert ranks == sorted(ranks)  # worst first
    assert code == max({"Healthy": 0, "Watch": 1, "Degraded": 1, "Critical": 2}[f["status"]] for f in d["fans"])
    st = d["fans"][0]["station"]
    by_station, _ = js(cli_home, "fleet", "--station", st.lower())
    assert {f["station"] for f in by_station["fans"]} == {st}
    healthy, code = js(cli_home, "fleet", "--status", "healthy")
    assert all(f["status"] == "Healthy" for f in healthy["fans"]) and code == 0
    assert call(cli_home, "fleet", "--status", "sleepy").exit_code == 3
    human = call(cli_home, "fleet")
    assert "HI trend" in human.output and any(ch in human.output for ch in "▁▂▃▄▅▆▇█")
    assert call(cli_home, "fleet", "list", "--json").exit_code in (0, 1, 2)  # limit view still available


def test_explain(cli_home):
    fleet, _ = js(cli_home, "fleet")
    worst = fleet["fans"][0]["fan_id"]
    d, _ = js(cli_home, "explain", "--fan", worst)
    assert {"contributing_features", "similar_past_failures", "fault_modes", "health"} <= set(d)
    for s in d["similar_past_failures"]:
        assert 0.5 <= s["similarity"] <= 1 and s["failure_mode"]
    assert "Explanation" in call(cli_home, "explain", "--fan", worst).output
