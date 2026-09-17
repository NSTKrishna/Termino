"""Source → sanitise → validate → idempotent upsert. See DESIGN.md §5 and §7."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from . import db
from .models import MEASUREMENTS, FailureRecord, Fan, MaintenanceRecord, PMTaskRow, TestRun
from .sources.base import RawRecord, Source

SENTINELS = {-999.0, -9999.0, 999.0, 9999.0, -99999.0, 99999.0}

_MODELS: dict[str, tuple[type[BaseModel], str, Any]] = {
    "fan": (Fan, "fan", db.fan_row),
    "pm_task": (PMTaskRow, "pm_task", db.pm_task_row),
    "test_run": (TestRun, "test_run", db.test_run_row),
    "maintenance": (MaintenanceRecord, "maintenance", db.maintenance_row),
    "failure": (FailureRecord, "failure", db.failure_row),
}


@dataclass
class IngestReport:
    source: str
    by_kind: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(Counter))
    values_nulled: int = 0
    ignored_columns: dict[str, list[str]] = field(default_factory=dict)
    rejected_examples: list[dict[str, str]] = field(default_factory=list)

    def total(self, outcome: str) -> int:
        return sum(c.get(outcome, 0) for c in self.by_kind.values())

    def to_dict(self) -> dict:
        d = {
            "source": self.source,
            "by_kind": {k: dict(v) for k, v in self.by_kind.items()},
            "values_nulled": self.values_nulled,
            "ignored_columns": self.ignored_columns,
            "rejected_examples": self.rejected_examples,
        }
        d["totals"] = {o: self.total(o) for o in ("inserted", "updated", "unchanged", "rejected")}
        return d


def _entity_key(kind: str, data: dict[str, Any]) -> str:
    keys = {"fan": ("fan_id",), "pm_task": ("fan_id", "task"), "test_run": ("fan_id", "timestamp", "run_type"),
            "maintenance": ("fan_id", "date", "task"), "failure": ("fan_id", "date", "failure_mode")}[kind]
    return "@".join(str(data.get(k)) for k in keys)


def sanitize_measurements(data: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, Any, str]]]:
    """Null values that are unparseable or outside sensor range. Returns (clean, issues)."""
    clean = dict(data)
    issues: list[tuple[str, Any, str]] = []
    for name, m in MEASUREMENTS.items():
        raw = clean.get(name)
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            issues.append((name, raw, "unparseable"))
            clean[name] = None
            continue
        if math.isnan(v):
            clean[name] = None
        elif v in SENTINELS:
            issues.append((name, raw, "sentinel_value"))
            clean[name] = None
        elif not (m.min <= v <= m.max):
            issues.append((name, raw, "out_of_sensor_range"))
            clean[name] = None
        else:
            clean[name] = v
    return clean, issues


def ingest(conn: sqlite3.Connection, source: Source) -> IngestReport:
    report = IngestReport(source=source.name)
    batch_id = conn.execute(
        "INSERT INTO ingest_batch(source, started_at) VALUES (?, ?)", (source.name, db.now_iso())
    ).lastrowid
    known_fans = db.fan_ids(conn)

    for rec in source.records():
        _ingest_one(conn, rec, source.name, known_fans, report)

    conn.execute(
        "UPDATE ingest_batch SET finished_at = ?, inserted = ?, updated = ?, unchanged = ?, rejected = ? WHERE id = ?",
        (db.now_iso(), report.total("inserted"), report.total("updated"), report.total("unchanged"),
         report.total("rejected"), batch_id),
    )
    conn.commit()
    return report


def _ingest_one(conn: sqlite3.Connection, rec: RawRecord, source: str, known_fans: set[str],
                report: IngestReport) -> None:
    model, table, to_row = _MODELS[rec.kind]
    data = {k: v for k, v in rec.data.items() if k in model.model_fields}
    extra = sorted(set(rec.data) - set(data))
    if extra:
        report.ignored_columns.setdefault(rec.kind, extra)
    key = _entity_key(rec.kind, data)

    def reject(field_name: str, raw: Any, issue: str) -> None:
        report.by_kind[rec.kind]["rejected"] += 1
        db.log_issue(conn, rec.kind, key, field_name, raw, issue, "rejected", f"{source} {rec.origin}")
        if len(report.rejected_examples) < 20:
            report.rejected_examples.append({"origin": rec.origin, "key": key, "field": field_name, "issue": issue})

    if rec.kind == "test_run":
        data, issues = sanitize_measurements(data)
        for field_name, raw, issue in issues:
            db.log_issue(conn, rec.kind, key, field_name, raw, issue, "nulled", f"{source} {rec.origin}")
        report.values_nulled += len(issues)

    if rec.kind != "fan" and data.get("fan_id") not in known_fans:
        reject("fan_id", data.get("fan_id"), "unknown_fan_id")
        return
    try:
        obj = model.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(p) for p in err["loc"]) or "record"
        reject(loc, err.get("input"), f"schema: {err['msg']}")
        return

    outcome = db.upsert(conn, table, to_row(obj), source)
    report.by_kind[rec.kind][outcome] += 1
    if rec.kind == "fan":
        known_fans.add(obj.fan_id)
