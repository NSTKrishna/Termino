"""End-to-end CLI behaviour through Typer's runner (every command supports --json)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from tbfmon.cli import app

runner = CliRunner()


def run(home, *args, ok=True):
    res = runner.invoke(app, ["--home", str(home), *args])
    if ok:
        assert res.exit_code == 0, res.output
    return res


def js(home, *args):
    return json.loads(run(home, *args, "--json").stdout)


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    h = tmp_path_factory.mktemp("home")
    run(h, "init")
    run(h, "simulate", "--fans", "12", "--years", "2", "--seed", "5")
    return h


def test_commands_require_init(tmp_path):
    res = run(tmp_path / "empty", "fleet", "list", "--json", ok=False)
    assert res.exit_code == 2
    assert "tbfmon init" in json.loads(res.stdout)["error"]


def test_init_is_idempotent_and_keeps_limits(tmp_path):
    first = js(tmp_path, "init")
    assert first["limits_written"] is True
    (tmp_path / "limits.yaml").write_text((tmp_path / "limits.yaml").read_text() + "\n# edited\n")
    again = js(tmp_path, "init")
    assert again["limits_written"] is False
    assert "# edited" in (tmp_path / "limits.yaml").read_text()


def test_simulate_json_and_files(tmp_path):
    run(tmp_path, "init")
    out = js(tmp_path, "simulate", "--fans", "8", "--years", "1", "--seed", "3")
    assert out["summary"]["fans"] == 8
    assert out["ingest"]["totals"]["inserted"] > 0
    assert (tmp_path / "sim" / "test_runs.csv").exists()
    assert (tmp_path / "ground_truth" / "ground_truth.csv").exists()


def test_ingest_after_simulate_is_noop(home):
    out = js(home, "ingest", str(home / "sim"))
    assert out["totals"]["inserted"] == 0 and out["totals"]["updated"] == 0
    assert out["totals"]["unchanged"] > 0


def test_ingest_refuses_ground_truth(home):
    res = run(home, "ingest", str(home / "ground_truth" / "ground_truth.csv"), "--kind", "failure", "--json", ok=False)
    assert res.exit_code == 2
    assert "ground truth" in json.loads(res.stdout)["error"]


def test_validate_json(home):
    res = run(home, "validate", "--json")
    out = json.loads(res.stdout)
    names = {c["name"]: c for c in out["checks"]}
    assert names["limits_file"]["status"] == "WARN"  # placeholders present
    assert names["capacity_classes"]["status"] == "PASS"
    assert names["isolated_spikes"]["count"] > 0  # injected glitches are found
    assert set(out["missing_pct"]) and out["status"] in ("PASS", "WARN")
    assert run(home, "validate", "--fail-on-warn", ok=False).exit_code == 1


def test_fleet_list_json_and_filters(home):
    out = js(home, "fleet", "list")
    assert len(out["fans"]) == 12
    row = out["fans"][0]
    assert {"condition", "pm_overdue", "suspect_parameters", "run_hours"} <= set(row)
    c45 = js(home, "fleet", "list", "--class", "C45")["fans"]
    assert all(r["capacity_class"] == "C45" for r in c45)
    ok = js(home, "fleet", "list", "--status", "ok")["fans"]
    assert all(r["condition"] == "OK" for r in ok)


def test_fan_show_json(home):
    out = js(home, "fan", "show", "tbf-001", "--runs", "6")
    assert out["fan"]["fan_id"] == "TBF-001"
    assert len(out["trend_run_dates"]) <= 6
    assert {r["parameter"] for r in out["readings"]} >= {"vib_de_rms_mms", "insulation_resistance_mohm"}
    assert out["pm"] and all("status" in t for t in out["pm"])


def test_fan_show_unknown(home):
    res = run(home, "fan", "show", "TBF-999", "--json", ok=False)
    assert res.exit_code == 1


def test_human_output_renders(home):
    for args in (["fleet", "list"], ["fan", "show", "TBF-002"], ["validate"]):
        res = run(home, *args, ok=args[0] != "validate")
        assert "Traceback" not in res.output
