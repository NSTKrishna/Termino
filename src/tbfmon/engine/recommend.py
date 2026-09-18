"""Layer 8 — maintenance recommendation per fan, compared with the OEM schedule.

Actions (decide_action):
* **urgent corrective**   — status Critical
* **targeted inspection** — status Degraded, or Watch with a named fault mode
* **defer OEM PM**        — Healthy, baseline established, recent data, and a
                            deferrable OEM task falls due within the horizon whose
                            related fault modes all have low confidence
* **no action**           — otherwise (OEM schedule continues unchanged)

Safety rules: with a stale fan (no run for `stale_after_days`) or no established
baseline, nothing is deferred. Insulation-resistance tests and control/starter
checks are never deferred (cheap, no height, and themselves condition checks).
A deferred task is postponed by `defer_factor` × interval but never beyond
`max_interval_factor` × interval since it was last done.

Savings compare OEM task occurrences in the horizon with the CBM plan for that
fan; man-hours per occurrence are assumptions in engine.yaml. Benefits of
prevented failures are NOT counted (conservative).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

import pandas as pd

from ..pm import add_months
from .config import RecommenderCfg

ACTIONS = ("no action", "defer OEM PM", "targeted inspection", "urgent corrective")


@dataclass
class Recommendation:
    fan_id: str
    action: str
    by_date: str | None
    tasks: list[str]
    needs_work_at_height: bool
    reason_codes: list[str]
    health_index: float | None
    status: str
    top_mode: str | None
    deferred: list[dict] = field(default_factory=list)
    oem_tasks_in_horizon: int = 0
    cbm_tasks_in_horizon: int = 0
    man_hours_oem: float = 0.0
    man_hours_cbm: float = 0.0
    height_hours_oem: float = 0.0
    height_hours_cbm: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def decide_action(status: str, top_mode: tuple[str, float] | None, established: bool, stale: bool,
                  min_conf: float) -> str:
    named = top_mode is not None and top_mode[1] >= min_conf
    if status == "Critical":
        return "urgent corrective"
    if status == "Degraded" or (status == "Watch" and named):
        return "targeted inspection"
    if status == "Healthy" and established and not stale:
        return "defer OEM PM"  # downgraded to "no action" if nothing is due / deferrable
    return "no action"


def _occurrences(last: dt.date | None, months: float, start: dt.date, end: dt.date) -> list[dt.date]:
    """Due dates in [start, end]; an overdue or never-recorded task is due at `start`."""
    due = start if last is None else add_months(last, int(round(months)))
    if due < start:
        due = start
    out = []
    while due <= end:
        out.append(due)
        due = add_months(due, int(round(months)))
    return out


def _matches(task: str, keywords: list[str]) -> str | None:
    t = task.lower()
    return next((k for k in keywords if k in t), None)


def recommend_fan(fan_id: str, status: str, hi: float | None, modes: list[tuple[str, float, str | None]],
                  trend_eta_days: float | None, established: bool, last_run: dt.date | None,
                  pm_tasks: pd.DataFrame, maintenance: pd.DataFrame, as_of: dt.date,
                  cfg: RecommenderCfg, min_conf: float, reasons: list[str]) -> Recommendation:
    stale = last_run is None or (as_of - last_run).days > cfg.stale_after_days
    top = (modes[0][0], modes[0][1]) if modes else None
    loc = (modes[0][2] if modes and modes[0][2] else "DE")
    action = decide_action(status, top, established, stale, min_conf)
    codes = list(reasons)
    if stale:
        codes.append(f"STALE_DATA:last_run={last_run}")
    horizon_end = as_of + dt.timedelta(days=cfg.horizon_days)
    mh = cfg.man_hours
    tasks: list[str] = []
    by: dt.date | None = None
    deferred: list[dict] = []

    # OEM occurrences vs CBM plan, task by task
    oem_n = cbm_n = 0
    mh_oem = mh_cbm = h_oem = h_cbm = 0.0
    done = maintenance[(maintenance["fan_id"] == fan_id) & (maintenance["type"] == "preventive")]
    conf = {m: c for m, c, _ in modes}
    for t in pm_tasks[pm_tasks["fan_id"] == fan_id].itertuples():
        if pd.isna(t.interval_months):
            continue
        prev = done[done["task"] == t.task]["date"]
        last = prev.max().date() if len(prev) else None
        occ = _occurrences(last, float(t.interval_months), as_of, horizon_end)
        per = mh["at_height"] if t.needs_work_at_height else mh["ground"]
        cbm_occ = occ
        kw = _matches(t.task, cfg.deferrable_tasks)
        related = cfg.task_modes.get(kw or "", [])
        if (action == "defer OEM PM" and kw and last is not None
                and all(conf.get(m, 0.0) < min_conf for m in related)):
            factor = min(1 + cfg.defer_factor, cfg.max_interval_factor)
            new_due = add_months(last, int(round(float(t.interval_months) * factor)))
            cbm_occ = _occurrences(last, float(t.interval_months) * factor, as_of, horizon_end)
            if len(cbm_occ) < len(occ):
                deferred.append({"task": t.task, "oem_due": occ[0].isoformat() if occ else None,
                                 "deferred_to": new_due.isoformat(), "needs_work_at_height": bool(t.needs_work_at_height),
                                 "evidence": f"no evidence of {', '.join(related) or 'related faults'}"})
        oem_n += len(occ)
        cbm_n += len(cbm_occ)
        mh_oem += len(occ) * per
        mh_cbm += len(cbm_occ) * per
        h_oem += len(occ) * per * bool(t.needs_work_at_height)
        h_cbm += len(cbm_occ) * per * bool(t.needs_work_at_height)

    if action == "defer OEM PM":
        if deferred:
            tasks = [f"Defer '{d['task']}' to {d['deferred_to']}" for d in deferred]
            by = min(dt.date.fromisoformat(d["deferred_to"]) for d in deferred)
            codes.append("HEALTHY_DEFER")
        else:
            action = "no action"
    if action == "urgent corrective":
        by = as_of + dt.timedelta(days=cfg.urgent_within_days)
        if top and top[1] >= min_conf:
            tasks = [s.format(loc=loc) for s in cfg.corrective.get(top[0], [])]
        tasks = tasks or ["Investigate parameters in ALARM before next drill; isolate fan if unsafe"]
        extra = mh["urgent_corrective"]
        mh_cbm += extra
        h_cbm += extra
    elif action == "targeted inspection":
        days = cfg.inspect_within_days if status in ("Degraded", "Critical") else cfg.watch_inspect_within_days
        if trend_eta_days is not None and trend_eta_days > 0:
            days = min(days, max(7, int(trend_eta_days * 0.5)))
        by = as_of + dt.timedelta(days=days)
        if top and top[1] >= min_conf:
            tasks = [s.format(loc=loc) for s in cfg.inspections.get(top[0], [])]
        tasks = tasks or ["General condition inspection of flagged parameters; repeat test run"]
        mh_cbm += mh["targeted_inspection"]
        h_cbm += mh["targeted_inspection"]
    elif action == "no action":
        codes.append("FOLLOW_OEM_SCHEDULE" if (stale or not established) else "MONITOR")

    needs_height = action in ("urgent corrective", "targeted inspection") or any(
        d["needs_work_at_height"] for d in deferred)
    return Recommendation(
        fan_id, action, by.isoformat() if by else None, tasks, needs_height, codes, hi, status,
        top[0] if top and top[1] >= min_conf else None, deferred, oem_n, cbm_n,
        round(mh_oem, 1), round(mh_cbm, 1), round(h_oem, 1), round(h_cbm, 1))


def totals(recs: list[Recommendation]) -> dict:
    s = lambda k: round(sum(getattr(r, k) for r in recs), 1)  # noqa: E731
    return {
        "fans": len(recs),
        "actions": {a: sum(r.action == a for r in recs) for a in ACTIONS},
        "oem_tasks_in_horizon": s("oem_tasks_in_horizon"),
        "cbm_tasks_in_horizon": s("cbm_tasks_in_horizon"),
        "tasks_deferred": sum(len(r.deferred) for r in recs),
        "man_hours_oem": s("man_hours_oem"),
        "man_hours_cbm": s("man_hours_cbm"),
        "man_hours_saved": round(s("man_hours_oem") - s("man_hours_cbm"), 1),
        "height_hours_oem": s("height_hours_oem"),
        "height_hours_cbm": s("height_hours_cbm"),
        "height_hours_saved": round(s("height_hours_oem") - s("height_hours_cbm"), 1),
    }
