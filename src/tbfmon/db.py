"""SQLite storage: schema, idempotent upserts and read helpers. See DESIGN.md §3."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .models import MEASUREMENT_FIELDS, FailureRecord, Fan, MaintenanceRecord, PMTaskRow, TestRun

SCHEMA_VERSION = 1

_MEAS_COLS = ",\n    ".join(f"{m} REAL" for m in MEASUREMENT_FIELDS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fan (
    fan_id TEXT PRIMARY KEY,
    station TEXT NOT NULL,
    line TEXT,
    tunnel_section TEXT NOT NULL,
    capacity_class TEXT NOT NULL,
    rated_power_kw REAL NOT NULL,
    rated_airflow_m3s REAL NOT NULL,
    rated_diff_pressure_pa REAL NOT NULL,
    rated_rpm REAL NOT NULL,
    rated_voltage_v REAL NOT NULL,
    rated_current_a REAL NOT NULL,
    oem_model TEXT NOT NULL,
    install_date TEXT NOT NULL,
    source TEXT,
    record_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_task (
    fan_id TEXT NOT NULL REFERENCES fan(fan_id),
    task TEXT NOT NULL,
    interval_months INTEGER,
    interval_run_hours REAL,
    needs_work_at_height INTEGER NOT NULL,
    source TEXT,
    record_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (fan_id, task)
);
CREATE TABLE IF NOT EXISTS test_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    fan_id TEXT NOT NULL REFERENCES fan(fan_id),
    ts TEXT NOT NULL,
    run_type TEXT NOT NULL,
    duration_min REAL NOT NULL,
    {_MEAS_COLS},
    source TEXT,
    record_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE (fan_id, ts, run_type)
);
CREATE INDEX IF NOT EXISTS ix_test_run_fan_ts ON test_run(fan_id, ts);
CREATE TABLE IF NOT EXISTS maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fan_id TEXT NOT NULL REFERENCES fan(fan_id),
    date TEXT NOT NULL,
    task TEXT NOT NULL,
    type TEXT NOT NULL,
    parts_replaced TEXT NOT NULL,
    findings TEXT NOT NULL,
    man_hours REAL,
    work_at_height INTEGER,
    source TEXT,
    record_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE (fan_id, date, task)
);
CREATE TABLE IF NOT EXISTS failure (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fan_id TEXT NOT NULL REFERENCES fan(fan_id),
    date TEXT NOT NULL,
    failure_mode TEXT NOT NULL,
    symptoms TEXT NOT NULL,
    root_cause TEXT NOT NULL,
    fix TEXT NOT NULL,
    downtime_hours REAL,
    source TEXT,
    record_hash TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE (fan_id, date, failure_mode)
);
CREATE TABLE IF NOT EXISTS data_quality_issue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    field TEXT NOT NULL,
    raw_value TEXT,
    issue TEXT NOT NULL,
    action TEXT NOT NULL,
    source TEXT,
    detected_at TEXT NOT NULL,
    UNIQUE (entity, entity_key, field, issue)
);
CREATE TABLE IF NOT EXISTS ingest_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0
);
"""

# Natural keys (DESIGN.md §3).
KEYS: dict[str, tuple[str, ...]] = {
    "fan": ("fan_id",),
    "pm_task": ("fan_id", "task"),
    "test_run": ("fan_id", "ts", "run_type"),
    "maintenance": ("fan_id", "date", "task"),
    "failure": ("fan_id", "date", "failure_mode"),
}


class NotInitialized(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def connect(path: Path, create: bool = False) -> sqlite3.Connection:
    if not create and not path.exists():
        raise NotInitialized(f"no database at {path}; run `tbfmon init` first")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Model → row conversion and hashing
# --------------------------------------------------------------------------


def _norm(v: Any) -> Any:
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def record_hash(row: dict[str, Any]) -> str:
    payload = {k: _norm(v) for k, v in row.items() if k not in ("source", "record_hash", "ingested_at")}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def fan_row(f: Fan) -> dict[str, Any]:
    d = f.model_dump(exclude={"pm_schedule"})
    return {k: _norm(v) for k, v in d.items()}


def pm_task_row(t: PMTaskRow) -> dict[str, Any]:
    return {k: _norm(v) for k, v in t.model_dump().items()}


def test_run_row(r: TestRun) -> dict[str, Any]:
    d = r.model_dump()
    d["ts"] = d.pop("timestamp")
    return {k: _norm(v) for k, v in d.items()}


def maintenance_row(m: MaintenanceRecord) -> dict[str, Any]:
    d = {k: _norm(v) for k, v in m.model_dump().items()}
    d["parts_replaced"] = json.dumps(m.parts_replaced, ensure_ascii=False)
    return d


def failure_row(f: FailureRecord) -> dict[str, Any]:
    d = {k: _norm(v) for k, v in f.model_dump(exclude={"symptoms"}).items()}
    d["symptoms"] = json.dumps([s.model_dump(mode="json") for s in f.symptoms], ensure_ascii=False)
    return d


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], source: str | None) -> str:
    """Insert or update by natural key. Returns 'inserted' | 'updated' | 'unchanged'."""
    keys = KEYS[table]
    h = record_hash(row)
    where = " AND ".join(f"{k} = ?" for k in keys)
    key_vals = [row[k] for k in keys]
    existing = conn.execute(f"SELECT record_hash FROM {table} WHERE {where}", key_vals).fetchone()
    if existing is not None and existing["record_hash"] == h:
        return "unchanged"
    full = {**row, "source": source, "record_hash": h, "ingested_at": now_iso()}
    if existing is None:
        cols = ", ".join(full)
        marks = ", ".join("?" for _ in full)
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(full.values()))
        return "inserted"
    sets = ", ".join(f"{k} = ?" for k in full if k not in keys)
    vals = [v for k, v in full.items() if k not in keys]
    conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", vals + key_vals)
    return "updated"


def log_issue(
    conn: sqlite3.Connection,
    entity: str,
    entity_key: str,
    field: str,
    raw_value: Any,
    issue: str,
    action: str,
    source: str | None,
) -> None:
    conn.execute(
        "INSERT INTO data_quality_issue(entity, entity_key, field, raw_value, issue, action, source, detected_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(entity, entity_key, field, issue) DO NOTHING",
        (entity, entity_key, field, None if raw_value is None else str(raw_value), issue, action, source, now_iso()),
    )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def fan_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT fan_id FROM fan")}


def get_fan(conn: sqlite3.Connection, fan_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM fan WHERE fan_id = ?", (fan_id,)).fetchone()
    return _clean(dict(r)) if r else None


def list_fans(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [_clean(dict(r)) for r in conn.execute("SELECT * FROM fan ORDER BY fan_id")]


def pm_tasks(conn: sqlite3.Connection, fan_id: str | None = None) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM pm_task", ()
    if fan_id:
        q, args = q + " WHERE fan_id = ?", (fan_id,)
    rows = [_clean(dict(r)) for r in conn.execute(q + " ORDER BY fan_id, task", args)]
    for r in rows:
        r["needs_work_at_height"] = bool(r["needs_work_at_height"])
    return rows


def runs_df(conn: sqlite3.Connection, fan_id: str | None = None) -> pd.DataFrame:
    q, args = "SELECT * FROM test_run", ()
    if fan_id:
        q, args = q + " WHERE fan_id = ?", (fan_id,)
    df = pd.read_sql_query(q + " ORDER BY fan_id, ts", conn, params=args)
    df["ts"] = pd.to_datetime(df["ts"])
    for m in MEASUREMENT_FIELDS:
        df[m] = df[m].astype("float64")
    return df.drop(columns=["record_hash", "ingested_at"])


def maintenance(conn: sqlite3.Connection, fan_id: str | None = None) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM maintenance", ()
    if fan_id:
        q, args = q + " WHERE fan_id = ?", (fan_id,)
    rows = [_clean(dict(r)) for r in conn.execute(q + " ORDER BY fan_id, date, task", args)]
    for r in rows:
        r["parts_replaced"] = json.loads(r["parts_replaced"])
        if r["work_at_height"] is not None:
            r["work_at_height"] = bool(r["work_at_height"])
    return rows


def failures(conn: sqlite3.Connection, fan_id: str | None = None) -> list[dict[str, Any]]:
    q, args = "SELECT * FROM failure", ()
    if fan_id:
        q, args = q + " WHERE fan_id = ?", (fan_id,)
    rows = [_clean(dict(r)) for r in conn.execute(q + " ORDER BY fan_id, date", args)]
    for r in rows:
        r["symptoms"] = json.loads(r["symptoms"])
    return rows


def issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM data_quality_issue ORDER BY id")]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables: Iterable[str] = ("fan", "pm_task", "test_run", "maintenance", "failure", "data_quality_issue")
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    for k in ("record_hash", "ingested_at"):
        d.pop(k, None)
    return d
