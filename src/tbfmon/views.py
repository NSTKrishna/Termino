"""Plain-data views behind `fleet list` and `fan show` (rendered as Rich or JSON)."""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
from typing import Any

import pandas as pd

from . import db
from .limits import Limit, LimitsFile, Status, describe, evaluate, worst
from .models import MEASUREMENT_FIELDS, MEASUREMENTS
from .pm import pm_status
from .validate import repair_dates, suspect_points

LATEST_WINDOW_RUNS = 3  # a parameter's "latest" value must come from one of the last N runs


def _f(v) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def latest_readings(runs: pd.DataFrame, as_of: dt.date, repairs: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Per parameter: the most recent trustworthy value within the last few runs up to `as_of`.

    Values flagged by `suspect_points` are skipped for the condition verdict; if the newest
    value is suspect it is reported under "suspect" so it is never silently dropped.
    """
    hist = runs[runs["ts"].dt.date <= as_of].sort_values("ts")
    window = list(hist.tail(LATEST_WINDOW_RUNS).index)
    out: dict[str, dict[str, Any]] = {}
    for p in MEASUREMENT_FIELDS:
        cand = [i for i in window if pd.notna(hist.at[i, p])]
        if not cand:
            continue
        suspects = suspect_points(hist, p, repairs=repairs)
        entry: dict[str, Any] = {}
        confirmed = [i for i in cand if i not in suspects]
        if confirmed:
            c = confirmed[-1]
            entry.update(value=float(hist.at[c, p]), ts=hist.at[c, "ts"].isoformat(),
                         ambient=_f(hist.at[c, "ambient_temp_c"]))
        if cand[-1] in suspects:
            n = cand[-1]
            entry["suspect"] = {"value": float(hist.at[n, p]), "ts": hist.at[n, "ts"].isoformat(),
                                "ambient": _f(hist.at[n, "ambient_temp_c"]), **suspects[n]}
        out[p] = entry
    return out


def condition(fan: dict[str, Any], runs: pd.DataFrame, limits: dict[str, Limit], as_of: dt.date,
              repairs: list[str] | None = None) -> dict[str, Any]:
    latest = latest_readings(runs, as_of, repairs)
    per_param = {}
    for p, lim in limits.items():
        rd = latest.get(p, {})
        st, x = evaluate(p, rd.get("value"), lim, fan, rd.get("ambient"))
        entry = {"status": st, "compared": x}
        if "suspect" in rd:
            sp = rd["suspect"]
            entry["suspect_status"] = evaluate(p, sp["value"], lim, fan, sp["ambient"])[0]
        per_param[p] = entry
    ranked = sorted(((v["status"], p) for p, v in per_param.items()), key=lambda t: -t[0].rank)
    overall = worst(v["status"] for v in per_param.values())  # suspect values excluded
    flagged = [p for s, p in ranked if s in (Status.WARN, Status.ALARM)]
    suspect = sorted(p for p, v in latest.items() if "suspect" in v)
    return {"status": overall, "flagged": flagged, "suspect": suspect, "latest": latest, "per_param": per_param}


def fleet_rows(conn: sqlite3.Connection, limits: LimitsFile, as_of: dt.date) -> list[dict[str, Any]]:
    runs = db.runs_df(conn)
    maint = db.maintenance(conn)
    fails = db.failures(conn)
    tasks = db.pm_tasks(conn)
    rows = []
    for fan in db.list_fans(conn):
        fid = fan["fan_id"]
        fr = runs[(runs.fan_id == fid) & (runs["ts"].dt.date <= as_of)]
        fm = [m for m in maint if m["fan_id"] == fid]
        ff = [f for f in fails if f["fan_id"] == fid and f["date"] <= as_of.isoformat()]
        try:
            cond = condition(fan, fr, limits.for_class(fan["capacity_class"]), as_of,
                             repair_dates(fm).get(fid))
        except KeyError:
            cond = {"status": Status.NA, "flagged": ["no limits for class"], "suspect": []}
        pm = pm_status([t for t in tasks if t["fan_id"] == fid], fm, fr, as_of)
        overdue = [t for t in pm if t["status"] == "OVERDUE"]
        rows.append({
            "fan_id": fid,
            "station": fan["station"],
            "line": fan["line"],
            "tunnel_section": fan["tunnel_section"],
            "capacity_class": fan["capacity_class"],
            "rated_power_kw": fan["rated_power_kw"],
            "install_date": fan["install_date"],
            "runs": int(len(fr)),
            "run_hours": round(float(fr["duration_min"].sum()) / 60.0, 1),
            "last_run": fr["ts"].max().date().isoformat() if len(fr) else None,
            "condition": cond["status"].value,
            "flagged_parameters": cond["flagged"],
            "suspect_parameters": cond["suspect"],
            "pm_tasks": len(pm),
            "pm_overdue": len(overdue),
            "pm_overdue_at_height": sum(t["needs_work_at_height"] for t in overdue),
            "failures": len(ff),
            "last_failure": ff[-1]["date"] if ff else None,
        })
    return rows


def fan_detail(conn: sqlite3.Connection, fan_id: str, limits: LimitsFile, as_of: dt.date,
               n_runs: int = 12) -> dict[str, Any] | None:
    fan = db.get_fan(conn, fan_id)
    if fan is None:
        return None
    runs = db.runs_df(conn, fan_id)
    runs = runs[runs["ts"].dt.date <= as_of]
    maint = db.maintenance(conn, fan_id)
    fails = db.failures(conn, fan_id)
    try:
        lims = limits.for_class(fan["capacity_class"])
    except KeyError:
        lims = {}
    cond = condition(fan, runs, lims, as_of, repair_dates(maint).get(fan_id))
    recent = runs.sort_values("ts").tail(n_runs)
    # Reference = median of the fan's first 5 values (earliest data we hold, not an OEM baseline).
    readings = []
    for p in MEASUREMENT_FIELDS:
        m = MEASUREMENTS[p]
        rd = cond["latest"].get(p, {})
        pp = cond["per_param"].get(p, {"status": Status.NA, "compared": None})
        ref = runs[p].dropna().head(5)
        sp = rd.get("suspect")
        readings.append({
            "parameter": p, "label": m.label, "unit": m.unit,
            "latest": rd.get("value"), "latest_ts": rd.get("ts"),
            "suspect": None if sp is None else {**sp, "status_if_true": pp.get("suspect_status", Status.NA).value},
            "reference_median_first5": float(ref.median()) if len(ref) else None,
            "limit": describe(lims[p]) if p in lims else None,
            "limit_placeholder": lims[p].placeholder if p in lims else None,
            "compared_value": pp["compared"], "status": pp["status"].value,
            "trend": [_f(v) for v in recent[p].tolist()],
        })
    tasks = db.pm_tasks(conn, fan_id)
    issues = conn.execute("SELECT COUNT(*) FROM data_quality_issue WHERE entity_key LIKE ?",
                          (f"{fan_id}@%",)).fetchone()[0]
    return {
        "fan": fan,
        "as_of": as_of.isoformat(),
        "age_years": round((as_of - dt.date.fromisoformat(fan["install_date"])).days / 365.25, 1),
        "runs": int(len(runs)),
        "run_hours": round(float(runs["duration_min"].sum()) / 60.0, 2),
        "first_run": runs["ts"].min().isoformat() if len(runs) else None,
        "last_run": runs["ts"].max().isoformat() if len(runs) else None,
        "run_types": runs["run_type"].value_counts().to_dict(),
        "condition": cond["status"].value,
        "flagged_parameters": cond["flagged"],
        "suspect_parameters": cond["suspect"],
        "readings": readings,
        "trend_run_dates": [t.date().isoformat() for t in recent["ts"]],
        "pm": pm_status(tasks, maint, runs, as_of),
        "maintenance": [m for m in maint if m["date"] <= as_of.isoformat()],
        "failures": [f for f in fails if f["date"] <= as_of.isoformat()],
        "data_quality_issues": issues,
    }
