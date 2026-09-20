"""plan (horizon parsing, grouping, deferrals, savings) and report (md/html/csv/json files)."""

from __future__ import annotations

import csv
import io
import json

import pytest
from typer.testing import CliRunner

from tbfmon.cli import app
from tbfmon.services.errors import TbfmonError
from tbfmon.services.plan import parse_horizon

runner = CliRunner()


def call(home, *args):
    return runner.invoke(app, ["--home", str(home), *args])


@pytest.mark.parametrize("text,days", [("90d", 90), ("90", 90), ("12w", 84), ("6m", 180), ("1y", 365), (" 2W ", 14)])
def test_parse_horizon(text, days):
    assert parse_horizon(text) == days


@pytest.mark.parametrize("bad", ["0d", "soon", "-5d", "9y", "3q"])
def test_parse_horizon_rejects(bad):
    with pytest.raises(TbfmonError):
        parse_horizon(bad)


def test_plan_json(cli_home):
    res = call(cli_home, "plan", "--horizon", "90d", "--json")
    assert res.exit_code in (0, 1, 2)
    p = json.loads(res.stdout)
    assert p["horizon_days"] == 90 and p["stations"]
    t = p["totals"]
    assert t["man_hours_saved"] == pytest.approx(t["man_hours_oem"] - t["man_hours_cbm"])
    assert res.exit_code == (2 if t["actions"].get("urgent corrective") else 1 if t["actions"].get("targeted inspection") else 0)
    for st in p["stations"]:
        dues = [i["due"] for i in st["items"]]
        assert dues == sorted(dues)
        assert all(p["as_of"] <= d <= p["end"] for d in dues)
        assert st["height_tasks"] == sum(i["needs_work_at_height"] for i in st["items"])
    assert all("insulation" not in d["task"].lower() for d in p["deferred"])  # never deferred
    deferred = {(d["fan_id"], d["task"]) for d in p["deferred"]}
    planned = {(i["fan_id"], i["task"]) for st in p["stations"] for i in st["items"] if i["kind"] == "OEM PM"}
    assert not deferred & planned
    longer = json.loads(call(cli_home, "plan", "--horizon", "1y", "--json").stdout)["totals"]
    assert longer["man_hours_oem"] > t["man_hours_oem"]


def test_plan_station_filter_and_errors(cli_home):
    fleet = json.loads(call(cli_home, "fleet", "--json").stdout)["fans"]
    st = fleet[0]["station"]
    p = json.loads(call(cli_home, "plan", "--station", st, "--json").stdout)
    assert [s["station"] for s in p["stations"]] == [st]
    assert call(cli_home, "plan", "--horizon", "tomorrow").exit_code == 3
    human = call(cli_home, "plan", "--horizon", "6m")
    assert "OEM schedule" in human.output and "work-at-height hours" in human.output


@pytest.mark.parametrize("fmt", ["md", "html", "csv", "json"])
def test_fleet_report_formats(cli_home, tmp_path, fmt):
    target = tmp_path / f"fleet.{fmt}"
    res = call(cli_home, "report", "--fleet", "--format", fmt, "--out", str(target), "--json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["report"] == str(target)
    text = target.read_text()
    if fmt == "md":
        assert text.startswith("# Fleet condition report") and "SIMULATED" in text and "| Fan |" in text
    elif fmt == "html":
        assert text.startswith("<!doctype html>") and "<script" not in text and "SIMULATED" in text
    elif fmt == "csv":
        rows = list(csv.DictReader(io.StringIO(text)))
        assert len(rows) == 16 and {"fan_id", "health_index", "status", "action"} <= set(rows[0])
    else:
        d = json.loads(text)
        assert len(d["fleet"]) == 16 and "plan" in d


def test_fan_report_and_stdout(cli_home, tmp_path):
    res = call(cli_home, "report", "--fan", "TBF-002", "--format", "md", "--out", "-")
    assert res.exit_code == 0 and "# Fan condition report — TBF-002" in res.output
    assert "Readings vs permissible limits" in res.output and "Similar past failures" in res.output
    res = call(cli_home, "report", "--fan", "2", "--format", "csv", "--out", str(tmp_path))
    assert res.exit_code == 0
    written = list(tmp_path.glob("fan_TBF-002_*.csv"))
    assert written and "parameter,measured,unit" in written[0].read_text().splitlines()[0]


def test_report_errors(cli_home):
    assert call(cli_home, "report", "--format", "md").exit_code == 3                       # neither
    assert call(cli_home, "report", "--fan", "1", "--fleet").exit_code == 3                 # both
    assert call(cli_home, "report", "--fleet", "--format", "pdf").exit_code == 3
    assert call(cli_home, "report", "--fan", "TBF-777", "--format", "md").exit_code == 3
