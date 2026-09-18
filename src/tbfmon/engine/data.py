"""Engine inputs loaded from the database (never from ground truth)."""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .. import db
from ..limits import LimitsFile
from .features import GROUPS, MODE_RESETS


@dataclass
class FleetData:
    fans: pd.DataFrame          # index fan_id; nameplate columns
    runs: pd.DataFrame          # one row per run, sorted by fan_id, ts; column `segment`
    maintenance: pd.DataFrame   # fan_id, date (Timestamp), task, type, findings, ...
    failures: pd.DataFrame      # fan_id, date (Timestamp), failure_mode, ...
    pm_tasks: pd.DataFrame      # fan_id, task, interval_months, interval_run_hours, needs_work_at_height
    limits: LimitsFile
    as_of: dt.date

    def fan(self, fan_id: str) -> dict:
        return {"fan_id": fan_id, **self.fans.loc[fan_id].to_dict()}


def _frame(rows: list[dict], cols: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=cols)
    if "date" in df:
        df["date"] = pd.to_datetime(df["date"])
    return df


def repairs_with_modes(maintenance: pd.DataFrame, failures: pd.DataFrame, link_days: int = 30) -> pd.DataFrame:
    """Corrective repairs with the failure mode they fixed (a failure record of the same fan
    up to `link_days` earlier); None when no failure record explains the repair."""
    corr = maintenance[maintenance["type"] == "corrective"][["fan_id", "date"]].copy()
    modes = []
    for r in corr.itertuples():
        f = failures[(failures["fan_id"] == r.fan_id) & (failures["date"] <= r.date)
                     & (failures["date"] >= r.date - pd.Timedelta(days=link_days))]
        modes.append(f.sort_values("date")["failure_mode"].iloc[-1] if len(f) else None)
    corr["mode"] = modes
    return corr


def segment_ids(runs: pd.DataFrame, maintenance: pd.DataFrame, failures: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per run: `segment` (repairs so far of any kind) and seg_<group> (repairs so far that
    reset that feature group). A repair legitimately changes a fan's baseline — but only for
    what it physically touched (features.MODE_RESETS); unexplained repairs reset everything."""
    failures = failures if failures is not None else pd.DataFrame(columns=["fan_id", "date", "failure_mode"])
    reps = repairs_with_modes(maintenance, failures)
    out = pd.DataFrame(0, index=runs.index, columns=["segment", "seg_none", *[f"seg_{g}" for g in GROUPS]],
                       dtype="int64")
    for fan_id, rr in reps.groupby("fan_id"):
        mask = (runs["fan_id"] == fan_id).to_numpy()
        day = runs.loc[mask, "ts"].dt.normalize().to_numpy()
        for r in rr.itertuples():
            after = day >= np.datetime64(r.date.normalize())
            groups = MODE_RESETS.get(r.mode, GROUPS) if r.mode else GROUPS
            out.loc[mask, "segment"] += after
            for g in groups:
                out.loc[mask, f"seg_{g}"] += after
    return out


def load_fleet(conn: sqlite3.Connection, limits: LimitsFile, as_of: dt.date) -> FleetData:
    """Everything up to and including `as_of` (so nothing from the future leaks in)."""
    cutoff = pd.Timestamp(as_of) + pd.Timedelta(days=1)
    fans = pd.DataFrame(db.list_fans(conn)).set_index("fan_id")
    runs = db.runs_df(conn)
    runs = runs[runs["ts"] < cutoff].sort_values(["fan_id", "ts"], kind="stable").reset_index(drop=True)
    maint = _frame(db.maintenance(conn), ["fan_id", "date", "task", "type", "parts_replaced", "findings",
                                          "man_hours", "work_at_height"])
    maint = maint[maint["date"] < cutoff].reset_index(drop=True)
    fails = _frame(db.failures(conn), ["fan_id", "date", "failure_mode", "symptoms", "root_cause", "fix",
                                       "downtime_hours"])
    fails = fails[fails["date"] < cutoff].reset_index(drop=True)
    tasks = pd.DataFrame(db.pm_tasks(conn), columns=["fan_id", "task", "interval_months", "interval_run_hours",
                                                     "needs_work_at_height", "source"])
    runs = pd.concat([runs, segment_ids(runs, maint, fails)], axis=1)
    return FleetData(fans, runs, maint, fails, tasks, limits, as_of)
