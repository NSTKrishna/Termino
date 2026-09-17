"""SQLite schema and upsert semantics."""

from __future__ import annotations

from tbfmon import db
from tbfmon.models import TestRun


def test_schema_version(conn):
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    db.init_schema(conn)  # idempotent


def test_upsert_insert_unchanged_update(conn, fan):
    row = db.fan_row(fan)
    assert db.upsert(conn, "fan", row, "a") == "inserted"
    assert db.upsert(conn, "fan", row, "b") == "unchanged"  # provenance not hashed
    assert db.upsert(conn, "fan", {**row, "station": "Patel Chowk"}, "a") == "updated"
    assert db.get_fan(conn, "TBF-001")["station"] == "Patel Chowk"


def test_test_run_roundtrip(conn, fan):
    db.upsert(conn, "fan", db.fan_row(fan), None)
    r = TestRun(fan_id="TBF-001", timestamp="2024-01-01T02:00:00", run_type="scheduled_test",
                duration_min=12.5, vib_de_rms_mms=1.23)
    assert db.upsert(conn, "test_run", db.test_run_row(r), None) == "inserted"
    df = db.runs_df(conn, "TBF-001")
    assert len(df) == 1 and df.loc[0, "vib_de_rms_mms"] == 1.23
