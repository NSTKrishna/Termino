"""drill (mock-drill mode) and watch (folder feed) through the CLI."""

from __future__ import annotations

import json
import shutil

import pandas as pd
import pytest
from typer.testing import CliRunner

from tbfmon.cli import app

runner = CliRunner()


@pytest.fixture(scope="module")
def home(cli_home, tmp_path_factory):
    h = tmp_path_factory.mktemp("drill") / "home"
    shutil.copytree(cli_home, h)  # drills write to the DB: keep the shared workspace pristine
    return h


def call(home, *args):
    return runner.invoke(app, ["--home", str(home), *args])


def _station_with_4(home):
    fleet = json.loads(call(home, "fleet", "--json").stdout)["fans"]
    counts = pd.Series([f["station"] for f in fleet]).value_counts()
    st = counts.index[0]
    return st, sorted(f["fan_id"] for f in fleet if f["station"] == st)


def test_simulated_drill_finds_injected_faults(home):
    st, fans = _station_with_4(home)
    res = call(home, "drill", "--station", st.lower(), "--simulate", "--inject", f"{fans[0]}:bearing_wear",
               "--inject", f"{fans[1]}:insulation_degradation", "--seed", "3", "--json")
    assert res.exit_code == 2, res.output
    d = json.loads(res.stdout)
    assert d["station"] == st and len(d["fans"]) == len(fans)
    top = {f["fan_id"]: f for f in d["fans"][:2]}
    assert set(top) == {fans[0], fans[1]}  # injected fans ranked highest risk
    assert top[fans[0]]["fault_modes"][0]["mode"] == "bearing_wear"
    assert top[fans[1]]["fault_modes"][0]["mode"] == "insulation_degradation"
    new = [a for a in d["alerts"] if a["outcome"] == "new"]
    assert {a["fan_id"] for a in new} >= {fans[0], fans[1]}
    assert any(a["severity"] in ("ALARM", "CRITICAL") for a in new)

    # the same drill file again: ingest unchanged, alerts deduplicated, nothing new
    again = json.loads(call(home, "drill", "--station", st, "--file", d["file"], "--json").stdout)
    assert again["ingest"]["totals"]["inserted"] == 0
    assert {a["outcome"] for a in again["alerts"]} <= {"deduplicated", "suppressed"}

    human = call(home, "drill", "--station", st, "--file", d["file"])
    assert "Risk-sorted summary" in human.output and "Mock drill" in human.output


def test_drill_file_validation_and_skips(home, tmp_path):
    st, fans = _station_with_4(home)
    bad = tmp_path / "bad.csv"
    bad.write_text("fan,when\nTBF-001,2026-01-01\n")
    res = call(home, "drill", "--station", st, "--file", str(bad), "--json")
    assert res.exit_code == 3 and "missing column" in json.loads(res.stdout)["error"]
    assert call(home, "drill", "--station", "Atlantis", "--simulate").exit_code == 3
    assert call(home, "drill", "--station", st).exit_code == 3                       # neither --file nor --simulate
    assert call(home, "drill", "--station", st, "--file", str(bad), "--simulate").exit_code == 3
    assert call(home, "drill", "--station", st, "--simulate", "--inject", "TBF-001:gremlins").exit_code == 3
    other = next(f for f in json.loads(call(home, "fleet", "--json").stdout)["fans"] if f["station"] != st)["fan_id"]
    mixed = tmp_path / "mixed.csv"
    num = int(fans[2].split("-")[1])
    mixed.write_text("fan_id,timestamp,run_type,duration_min,vib_de_rms_mms,temp_bearing_de_c,ambient_temp_c\n"
                     f"tbf-{num},2027-03-01T02:00:00,mock_drill,30,1.3,52,30\n"
                     f"{other},2027-03-01T02:10:00,mock_drill,30,1.2,50,30\n"
                     "TBF-999,2027-03-01T02:20:00,mock_drill,30,1.2,50,30\n")
    d = json.loads(call(home, "drill", "--station", st, "--file", str(mixed), "--json").stdout)
    assert [f["fan_id"] for f in d["fans"]] == [fans[2]]  # "tbf-N" resolved, others skipped
    reasons = " ".join(s["reason"] for s in d["skipped"])
    assert f"{other} is not at {st}" in reasons and "unknown fan" in reasons
    assert any("missing" in n or "no drill reading" in n for n in d["notes"])
    assert d["fans"][0]["missing_sensors"]  # only vib DE and bearing temp were measured


def test_watch_once_processes_new_files_only_once(home, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    st, fans = _station_with_4(home)
    d = json.loads(call(home, "drill", "--station", st, "--simulate", "--seed", "9", "--json").stdout)
    shutil.copy(d["file"], inbox / "a.csv")
    (inbox / "broken.csv").write_text("not,a,reading\n1,2,3\n")
    res = call(home, "watch", str(inbox), "--once", "--json")
    assert res.exit_code in (0, 1, 2)
    events = [json.loads(line) for line in res.stdout.splitlines()]
    files = {e["file"]: e for e in events if e["event"] == "file"}
    assert files["a.csv"]["ok"] and len(files["a.csv"]["fans"]) == len(fans)
    assert not files["broken.csv"]["ok"] and "missing column" in files["broken.csv"]["error"]
    assert events[-1]["event"] == "summary" and events[-1]["files"] == 2
    again = [json.loads(line) for line in call(home, "watch", str(inbox), "--once", "--json").stdout.splitlines()]
    assert again == [{"event": "summary", "files": 0, "alerts": 0, "exit_code": 0}]


def test_watch_simulated_feed(home, tmp_path):
    res = call(home, "watch", str(tmp_path / "feed"), "--simulate-feed", "--feed-count", "2", "--feed-every", "0.3",
               "--interval", "0.2", "--duration", "20")
    assert res.exit_code in (0, 1, 2), res.output
    assert "processed 2 file(s)" in res.output
    assert len(list((tmp_path / "feed").glob("drill_*.csv"))) == 2


def test_watch_missing_directory(home, tmp_path):
    res = call(home, "watch", str(tmp_path / "nope"), "--once", "--json")
    assert res.exit_code == 3 and "not found" in json.loads(res.stdout)["error"]
