"""Services foundation: settings (YAML + env, IST), workspace lookups, schema migration, exit codes."""

from __future__ import annotations

import datetime as dt

import pytest
from typer.testing import CliRunner

from tbfmon import db
from tbfmon.cli import app
from tbfmon.ingest import ingest
from tbfmon.paths import Home
from tbfmon.services.errors import TbfmonError
from tbfmon.services.settings import DEFAULT_CONFIG, load_settings
from tbfmon.services.workspace import Workspace
from tbfmon.simulate import generate
from tbfmon.sources import SimulatorSource

runner = CliRunner()


def test_settings_defaults_yaml_and_env(tmp_path, monkeypatch):
    s = load_settings(tmp_path)
    assert s.timezone == "Asia/Kolkata" and s.alerts.cooldown_hours == 12
    assert s.fmt(dt.datetime(2025, 12, 31, 2, 14)) == "2025-12-31 02:14 IST"
    (tmp_path / "config.yaml").write_text(DEFAULT_CONFIG.replace("cooldown_hours: 12", "cooldown_hours: 3"))
    assert load_settings(tmp_path).alerts.cooldown_hours == 3
    monkeypatch.setenv("TBFMON_ALERT_COOLDOWN_HOURS", "1.5")
    monkeypatch.setenv("TBFMON_USER", "shift-engineer")
    monkeypatch.setenv("TBFMON_NOTIFY_DRY_RUN", "false")
    s = load_settings(tmp_path)
    assert s.alerts.cooldown_hours == 1.5 and s.who() == "shift-engineer" and s.alerts.email.dry_run is False


def test_settings_friendly_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("TBFMON_TZ", "Mars/Olympus")
    with pytest.raises(TbfmonError, match="unknown timezone"):
        load_settings(tmp_path)
    monkeypatch.delenv("TBFMON_TZ")
    (tmp_path / "config.yaml").write_text("alerts: [unclosed")
    with pytest.raises(TbfmonError, match="not valid YAML"):
        load_settings(tmp_path)


@pytest.fixture
def ws(tmp_path):
    c = db.connect(tmp_path / "tbfmon.db", create=True)
    db.init_schema(c)
    ingest(c, SimulatorSource(generate(12, 1, seed=2)))
    c.close()
    return Workspace.open(Home(tmp_path))


def test_workspace_fan_lookup_with_suggestions(ws):
    assert ws.fan_id("tbf-003") == "TBF-003" and ws.fan_id("7") == "TBF-007"
    with pytest.raises(TbfmonError) as e:
        ws.fan_id("TBF-0O3")
    assert "did you mean" in e.value.hint and "TBF-003" in e.value.hint


def test_workspace_station_lookup(ws):
    st = ws.stations()[0]
    assert ws.station(st.upper()) == st
    with pytest.raises(TbfmonError) as e:
        ws.station("Atlantis")
    assert e.value.hint


def test_workspace_requires_init_and_migrates(tmp_path):
    with pytest.raises(TbfmonError, match="no workspace"):
        Workspace.open(Home(tmp_path / "nope"))
    c = db.connect(tmp_path / "tbfmon.db", create=True)
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    c.commit()
    ws = Workspace.open(Home(tmp_path))
    assert ws.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    assert ws.conn.execute("SELECT COUNT(*) FROM alert").fetchone()[0] == 0


def test_usage_errors_exit_3_not_alarm(tmp_path):
    res = runner.invoke(app, ["--home", str(tmp_path), "simulate", "--fans", "abc"])
    assert res.exit_code == 3


def test_init_writes_settings_file(tmp_path):
    res = runner.invoke(app, ["--home", str(tmp_path), "init", "--json"])
    assert res.exit_code == 0 and (tmp_path / "config.yaml").exists()
