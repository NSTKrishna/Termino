"""OEM time-based PM status (the baseline that condition-based maintenance will improve on)."""

from __future__ import annotations

import calendar
import datetime as dt
import sqlite3
from typing import Any

import pandas as pd

DUE_SOON_DAYS = 30


def add_months(d: dt.date, months: int) -> dt.date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def data_as_of(conn: sqlite3.Connection) -> dt.date:
    """Latest date present in the data (default reference date for due/overdue)."""
    row = conn.execute(
        "SELECT MAX(d) FROM (SELECT MAX(substr(ts, 1, 10)) AS d FROM test_run "
        "UNION ALL SELECT MAX(date) FROM maintenance)"
    ).fetchone()
    return dt.date.fromisoformat(row[0]) if row and row[0] else dt.date.today()


def resolve_as_of(conn: sqlite3.Connection, as_of: str | None) -> tuple[dt.date, str]:
    if as_of is None or as_of == "data":
        return data_as_of(conn), "latest data"
    if as_of == "today":
        return dt.date.today(), "today"
    return dt.date.fromisoformat(as_of), "given"


def pm_status(tasks: list[dict[str, Any]], maint: list[dict[str, Any]], runs: pd.DataFrame,
              as_of: dt.date) -> list[dict[str, Any]]:
    """Per task: last done, next due by calendar and by run-hours, and status."""
    out = []
    for t in tasks:
        done = sorted(m["date"] for m in maint
                      if m["task"] == t["task"] and m["type"] == "preventive" and m["date"] <= as_of.isoformat())
        last = dt.date.fromisoformat(done[-1]) if done else None
        next_due = add_months(last, int(t["interval_months"])) if last and t.get("interval_months") else None
        hours_since = None
        if last is not None and len(runs):
            mask = (runs["ts"].dt.date > last) & (runs["ts"].dt.date <= as_of)
            hours_since = round(float(runs.loc[mask, "duration_min"].sum()) / 60.0, 2)
        hours_left = (round(t["interval_run_hours"] - hours_since, 1)
                      if t.get("interval_run_hours") and hours_since is not None else None)

        if last is None:
            status = "NO RECORD"
        elif (next_due and as_of > next_due) or (hours_left is not None and hours_left < 0):
            status = "OVERDUE"
        elif next_due and (next_due - as_of).days <= DUE_SOON_DAYS:
            status = "DUE SOON"
        else:
            status = "OK"
        out.append({
            "task": t["task"],
            "interval_months": t.get("interval_months"),
            "interval_run_hours": t.get("interval_run_hours"),
            "needs_work_at_height": bool(t["needs_work_at_height"]),
            "last_done": last.isoformat() if last else None,
            "next_due": next_due.isoformat() if next_due else None,
            "days_overdue": (as_of - next_due).days if next_due and as_of > next_due else 0,
            "run_hours_since": hours_since,
            "run_hours_left": hours_left,
            "status": status,
        })
    return out
