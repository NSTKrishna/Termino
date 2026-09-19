"""Alert store: generation, dedupe, escalation, cooldown, ack; channels; CLI list/ack."""

from __future__ import annotations

import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from tbfmon.cli import app
from tbfmon.paths import Home
from tbfmon.services.alerts import AlertCandidate, AlertStore, candidates_from_analysis, outcomes_exit
from tbfmon.services.errors import TbfmonError
from tbfmon.services.workspace import Workspace

runner = CliRunner()
IST = ZoneInfo("Asia/Kolkata")
T0 = dt.datetime(2026, 1, 5, 2, 0, tzinfo=IST)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.delenv("TBFMON_NOTIFY_DRY_RUN", raising=False)
    assert runner.invoke(app, ["--home", str(tmp_path), "init"]).exit_code == 0
    return Workspace.open(Home(tmp_path))


def cand(sev="WARN", key="TBF-001|limit|vib_de_rms_mms"):
    return AlertCandidate(key, "TBF-001", sev, f"TBF-001 vib {sev}", "measured 5.2 mm/s", "test")


def test_new_dedupe_escalate(ws):
    s = AlertStore(ws)
    assert s.raise_many([cand()], T0)[0].outcome == "new"
    o = s.raise_many([cand()], T0 + dt.timedelta(minutes=5))[0]
    assert o.outcome == "deduplicated" and o.notified == []
    o = s.raise_many([cand("ALARM")], T0 + dt.timedelta(minutes=10))[0]
    assert o.outcome == "escalated" and o.severity == "ALARM"
    rows = s.list()
    assert len(rows) == 1 and rows[0]["occurrences"] == 3 and rows[0]["severity"] == "ALARM"
    log = [json.loads(line) for line in (ws.home.root / "alerts.jsonl").read_text().splitlines()]
    assert [r["event"] for r in log] == ["new", "escalated"]
    assert log[0]["at"].endswith("+05:30")


def test_ack_cooldown_and_escalation_through_cooldown(ws):
    s = AlertStore(ws)
    aid = s.raise_many([cand()], T0)[0].alert_id
    acked = s.ack([aid], user="ravi", note="checked, re-test planned", now=T0 + dt.timedelta(hours=1))[0]
    assert acked["acked_by"] == "ravi" and acked["acked_at"].endswith("+05:30") and acked["status"] == "acked"
    assert s.raise_many([cand()], T0 + dt.timedelta(hours=5))[0].outcome == "suppressed"      # within 12 h cooldown
    assert s.raise_many([cand("ALARM")], T0 + dt.timedelta(hours=6))[0].outcome == "new"      # higher severity
    assert s.raise_many([cand(key="k2")], T0)[0].outcome == "new"
    s.ack([], all_open=True, now=T0 + dt.timedelta(hours=7))
    assert s.list("open") == []
    assert s.raise_many([cand("ALARM")], T0 + dt.timedelta(hours=20))[0].outcome == "new"     # cooldown over
    assert s.ack([aid], now=T0 + dt.timedelta(hours=21))[0]["result"] == "already acknowledged"
    with pytest.raises(TbfmonError, match="no alert with id"):
        s.ack([9999])


def test_notifier_stubs_dry_run_and_min_severity(ws, monkeypatch):
    cfg = (ws.home.root / "config.yaml").read_text().replace("email:   {enabled: false", "email:   {enabled: true")
    (ws.home.root / "config.yaml").write_text(cfg)
    ws2 = Workspace.open(ws.home)
    s = AlertStore(ws2)
    o = s.raise_many([cand("ALARM")], T0)[0]
    assert any(n.startswith("email: dry-run") for n in o.notified)
    assert "sms: disabled" in o.notified
    outbox = [json.loads(x) for x in (ws.home.root / "outbox.jsonl").read_text().splitlines()]
    assert outbox[0]["channel"] == "email" and outbox[0]["dry_run"] and "ALARM" in outbox[0]["subject"]
    info = s.raise_many([cand("INFO", key="k-info")], T0)[0]
    assert not any(n.startswith("email") for n in info.notified)  # below min_notify_severity
    monkeypatch.setenv("TBFMON_NOTIFY_DRY_RUN", "false")
    o = AlertStore(Workspace.open(ws.home)).raise_many([cand("ALARM", key="k3")], T0)[0]
    assert "email: NOT SENT (stub — no transport configured)" in o.notified


def _analysis(status="Critical", param_status="ALARM"):
    return {"fan_id": "TBF-002", "run_ts": "2026-01-05T02:00:00", "status": status, "health_index": 31.0,
            "params": [{"parameter": "vib_de_rms_mms", "label": "Vib DE RMS", "unit": "mm/s", "measured": 8.1,
                        "warn": 4.5, "alarm": 7.1, "deviation_pct": 80.0, "status": param_status},
                       {"parameter": "noise_dba", "label": "Noise", "unit": "dB(A)", "measured": 140.0,
                        "warn": 95, "alarm": 100, "deviation_pct": None, "status": "SUSPECT"}],
            "fault_modes": [{"mode": "bearing_wear", "confidence": 0.8, "location": "DE", "reasons": []}],
            "recommendation": {"action": "urgent corrective", "by_date": "2026-01-12"}}


def test_candidates_from_analysis():
    c = {x.key: x for x in candidates_from_analysis(_analysis(), "drill")}
    assert c["TBF-002|limit|vib_de_rms_mms"].severity == "ALARM"
    assert "+80% beyond WARN" in c["TBF-002|limit|vib_de_rms_mms"].message
    assert c["TBF-002|suspect|noise_dba"].severity == "INFO"
    assert c["TBF-002|health"].severity == "CRITICAL" and "bearing_wear" in c["TBF-002|health"].message
    assert not [x for x in candidates_from_analysis(_analysis("Healthy", "OK"), "drill") if x.severity != "INFO"]


def test_outcome_exit_codes(ws):
    s = AlertStore(ws)
    assert outcomes_exit(s.raise_many([cand("WARN")], T0)) == 1
    assert outcomes_exit(s.raise_many([cand("CRITICAL", key="c")], T0)) == 2


def test_cli_alerts_list_and_ack(ws):
    home = str(ws.home.root)
    empty = runner.invoke(app, ["--home", home, "alerts", "list", "--json"])
    assert empty.exit_code == 0 and json.loads(empty.stdout)["alerts"] == []
    AlertStore(ws).raise_many([cand("ALARM"), cand("INFO", key="k2")])
    res = runner.invoke(app, ["--home", home, "alerts", "list"])
    assert res.exit_code == 2 and "ALARM" in res.output
    only_alarm = json.loads(runner.invoke(app, ["--home", home, "alerts", "list", "--severity", "alarm", "--json"]).stdout)
    assert [a["severity"] for a in only_alarm["alerts"]] == ["ALARM"]
    aid = only_alarm["alerts"][0]["id"]
    res = runner.invoke(app, ["--home", home, "alerts", "ack", str(aid), "--user", "meena", "--note", "bearing ordered"])
    assert res.exit_code == 0 and "acknowledged by meena" in res.output and "IST" in res.output
    assert runner.invoke(app, ["--home", home, "alerts", "list"]).exit_code == 0  # only INFO left open
    assert runner.invoke(app, ["--home", home, "alerts", "ack", "424242"]).exit_code == 3
    assert runner.invoke(app, ["--home", home, "alerts", "ack"]).exit_code == 3
    assert runner.invoke(app, ["--home", home, "alerts", "list", "--status", "maybe"]).exit_code == 3
