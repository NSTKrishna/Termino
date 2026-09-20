"""Condition-based maintenance plan over a horizon, grouped by station, vs the OEM schedule."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from ..engine.recommend import oem_occurrences, recommend_fan
from .errors import TbfmonError
from .workspace import Workspace

UNITS = {"d": 1, "w": 7, "m": 30, "y": 365}
COMBINE_DAYS = 30  # OEM tasks related to the fault, due within this long after the visit, are done during it


def parse_horizon(text: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([dwmy]?)\s*", str(text).lower())
    if not m or int(m.group(1)) == 0:
        raise TbfmonError(f"bad horizon {text!r}", "use e.g. 90d, 12w, 6m or 1y")
    days = int(m.group(1)) * UNITS[m.group(2) or "d"]
    if days > 5 * 365:
        raise TbfmonError(f"horizon {text!r} is longer than 5 years", "plans beyond a few PM cycles are not meaningful")
    return days


@dataclass
class PlanItem:
    fan_id: str
    kind: str                    # urgent corrective | targeted inspection | OEM PM
    task: str
    due: str
    needs_work_at_height: bool
    man_hours: float
    reason: str = ""


@dataclass
class StationPlan:
    station: str
    items: list[PlanItem]
    man_hours: float
    height_tasks: int


@dataclass
class Plan:
    as_of: str
    horizon_days: int
    end: str
    stations: list[StationPlan]
    deferred: list[dict[str, Any]]
    totals: dict[str, Any]
    assumptions: dict[str, Any]
    exit_code: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_plan(ws: Workspace, horizon_days: int, as_of: str | None = None, station: str | None = None,
               fan: str | None = None) -> Plan:
    a = ws.assessment(as_of)
    cfg = a.cfg.recommender.model_copy(update={"horizon_days": horizon_days})
    start = a.data.as_of
    end = start + dt.timedelta(days=horizon_days)
    st = ws.station(station) if station else None
    only_fan = ws.fan_id(fan) if fan else None
    mh = cfg.man_hours
    per_station: dict[str, list[PlanItem]] = {}
    deferred: list[dict[str, Any]] = []
    tot = {"oem_tasks": 0, "cbm_tasks": 0, "man_hours_oem": 0.0, "man_hours_cbm": 0.0,
           "height_hours_oem": 0.0, "height_hours_cbm": 0.0, "actions": {}}
    notes = []
    combined: list[dict[str, Any]] = []
    maint = a.data.maintenance
    done = maint[maint["type"] == "preventive"]
    for fan_id, f in a.data.fans.iterrows():
        if (st and f["station"] != st) or (only_fan and fan_id != only_fan):
            continue
        i = a.latest_index(fan_id)
        if i is None:
            notes.append(f"{fan_id}: no test runs — OEM schedule applies unchanged")
            status, hi, modes, eta, est, last_run, reasons = "Watch", None, [], None, False, None, ["NO_RUNS"]
        else:
            row = a.table.loc[i]
            h = a.health[i]
            status, hi, modes = h.status, h.index, a.combined[i]
            eta = None if pd.isna(row["trend_eta_days"]) else float(row["trend_eta_days"])
            est, last_run, reasons = bool(row["baseline_established"]), row["ts"].date(), h.reasons
        rec = recommend_fan(fan_id, status, hi, modes, eta, est, last_run, a.data.pm_tasks, maint, start, cfg,
                            a.cfg.fault_modes.min_confidence, reasons)
        tot["actions"][rec.action] = tot["actions"].get(rec.action, 0) + 1
        for k, src in (("oem_tasks", "oem_tasks_in_horizon"), ("cbm_tasks", "cbm_tasks_in_horizon"),
                       ("man_hours_oem", "man_hours_oem"), ("man_hours_cbm", "man_hours_cbm"),
                       ("height_hours_oem", "height_hours_oem"), ("height_hours_cbm", "height_hours_cbm")):
            tot[k] += getattr(rec, src)
        items = per_station.setdefault(f["station"], [])
        why = ", ".join(c for c in rec.reason_codes if not c.startswith("LIMITED"))[:120]
        if rec.action in ("urgent corrective", "targeted inspection") and rec.by_date:
            hours = mh["urgent_corrective"] if rec.action == "urgent corrective" else mh["targeted_inspection"]
            items.append(PlanItem(fan_id, rec.action, "; ".join(rec.tasks), rec.by_date, True, hours, why))
        deferred_tasks = {d["task"] for d in rec.deferred}
        for d in rec.deferred:
            deferred.append({"fan_id": fan_id, "station": f["station"], **d})
        # OEM tasks that address the fault being corrected/inspected are done in the same visit.
        visit = rec.action in ("urgent corrective", "targeted inspection") and rec.by_date and rec.top_mode
        combine_until = (dt.date.fromisoformat(rec.by_date) + dt.timedelta(days=COMBINE_DAYS)) if visit else None
        for t in a.data.pm_tasks[a.data.pm_tasks["fan_id"] == fan_id].itertuples():
            if pd.isna(t.interval_months) or t.task in deferred_tasks:
                continue
            prev = done[(done["fan_id"] == fan_id) & (done["task"] == t.task)]["date"]
            last = prev.max().date() if len(prev) else None
            kw = next((k for k in cfg.task_modes if k in t.task.lower()), None)
            related = visit and kw is not None and rec.top_mode in cfg.task_modes[kw]
            hours = mh["at_height"] if t.needs_work_at_height else mh["ground"]
            for due in oem_occurrences(last, float(t.interval_months), start, end):
                if related and due <= combine_until:
                    tot["cbm_tasks"] -= 1
                    tot["man_hours_cbm"] -= hours
                    tot["height_hours_cbm"] -= hours * bool(t.needs_work_at_height)
                    combined.append({"fan_id": fan_id, "task": t.task, "due": due.isoformat(), "with": rec.action})
                    continue
                items.append(PlanItem(fan_id, "OEM PM", t.task, due.isoformat(), bool(t.needs_work_at_height), hours,
                                      "overdue" if last is None or due == start else "time-based schedule"))
    stations = []
    for name in sorted(per_station):
        items = sorted(per_station[name], key=lambda x: (x.due, {"urgent corrective": 0, "targeted inspection": 1}
                                                           .get(x.kind, 2), x.fan_id))
        stations.append(StationPlan(name, items, round(sum(x.man_hours for x in items), 1),
                                    sum(x.needs_work_at_height for x in items)))
    for k in ("man_hours_oem", "man_hours_cbm", "height_hours_oem", "height_hours_cbm"):
        tot[k] = round(tot[k], 1)
    tot["man_hours_saved"] = round(tot["man_hours_oem"] - tot["man_hours_cbm"], 1)
    tot["height_hours_saved"] = round(tot["height_hours_oem"] - tot["height_hours_cbm"], 1)
    tot["tasks_deferred"] = len(deferred)
    if combined:
        notes.append(f"{len(combined)} OEM task(s) combined with condition-driven visits: "
                     + ", ".join(f"{c['fan_id']} {c['task']}" for c in combined[:6]))
    code = 2 if tot["actions"].get("urgent corrective") else 1 if tot["actions"].get("targeted inspection") else 0
    assumptions = {"man_hours": dict(mh), "max_interval_factor": cfg.max_interval_factor,
                   "never_deferred": "insulation resistance tests, control/starter checks",
                   "not_counted": "benefit of failures prevented"}
    return Plan(start.isoformat(), horizon_days, end.isoformat(), stations, deferred, tot, assumptions, code, notes)
