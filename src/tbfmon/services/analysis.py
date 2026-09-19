"""Per-fan run analysis and the fleet health table (headless; see services/__init__)."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from ..engine import Assessment
from ..engine.recommend import Recommendation, recommend_fan
from ..limits import RATED_REF
from ..models import MEASUREMENTS
from .errors import STATUS_EXIT, TbfmonError
from .workspace import Workspace

# Instrument groups, used to tell the engineer which sensors were missing on a run.
SENSOR_GROUPS: dict[str, tuple[str, ...]] = {
    "vibration (portable analyser)": ("vib_de_rms_mms", "vib_nde_rms_mms", "vib_de_1x_mms", "vib_de_2x_mms",
                                      "vib_nde_1x_mms", "vib_nde_2x_mms"),
    "bearing RTDs": ("temp_bearing_de_c", "temp_bearing_nde_c"),
    "winding RTD": ("temp_winding_c",),
    "MCC meter (currents/voltage)": ("current_l1_a", "current_l2_a", "current_l3_a", "voltage_v"),
    "tachometer": ("rpm",),
    "airflow / ΔP": ("airflow_m3s", "diff_pressure_pa"),
    "sound level meter": ("noise_dba",),
    "insulation tester (megger)": ("insulation_resistance_mohm",),
    "start/stop (SCADA)": ("runup_time_s", "inrush_peak_a", "coastdown_time_s"),
    "ambient sensor": ("ambient_temp_c",),
}


def _num(v: Any) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


@dataclass
class ParamRow:
    parameter: str
    label: str
    unit: str
    measured: float | None
    warn: float | None           # in the measured unit
    alarm: float | None          # in the measured unit
    side: str | None             # "high" | "low"
    basis: str | None
    deviation_pct: float | None  # vs WARN, toward the bad side (> 0 = beyond WARN)
    status: str                  # OK | WARN | ALARM | SUSPECT | no limit | not measured


@dataclass
class FanAnalysis:
    fan_id: str
    station: str
    capacity_class: str
    run_id: int
    run_ts: str
    run_type: str
    duration_min: float
    health_index: float
    status: str
    components: dict[str, float]
    reasons: list[str]
    params: list[ParamRow]
    missing_sensors: list[str]
    fault_modes: list[dict[str, Any]]
    anomaly_percentile: float | None
    recommendation: dict[str, Any]
    is_latest_run: bool
    exit_code: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_measured_units(value: float | None, basis: str, param: str, fan: dict, ambient: float | None) -> float | None:
    if value is None:
        return None
    if basis == "pct_of_rated":
        rated = fan.get(RATED_REF.get(param, ""))
        return round(value / 100.0 * rated, 3) if rated else None
    if basis == "rise_over_ambient":
        return round(ambient + value, 2) if ambient is not None else None
    return value


def recommendation_for(a: Assessment, i: int, ws: Workspace) -> Recommendation:
    row = a.table.loc[i]
    h = a.health[i]
    return recommend_fan(
        row["fan_id"], h.status, h.index, a.combined[i],
        None if pd.isna(row["trend_eta_days"]) else float(row["trend_eta_days"]),
        bool(row["baseline_established"]), row["ts"].date(), a.data.pm_tasks, a.data.maintenance,
        a.data.as_of, a.cfg.recommender, a.cfg.fault_modes.min_confidence, h.reasons)


def run_ids(ws: Workspace, fan_id: str, n: int = 5) -> list[tuple[int, str]]:
    rows = ws.conn.execute("SELECT run_id, ts FROM test_run WHERE fan_id = ? ORDER BY ts DESC LIMIT ?",
                           (fan_id, n)).fetchall()
    return [(r[0], r[1]) for r in rows]


def analyze_fan(ws: Workspace, fan: str, run_id: int | None = None) -> FanAnalysis:
    fan_id = ws.fan_id(fan)
    if run_id is None:
        latest = run_ids(ws, fan_id, 1)
        if not latest:
            raise TbfmonError(f"{fan_id} has no test runs", "ingest a drill/test file first (`tbfmon drill` or `tbfmon ingest`)")
        run_id = latest[0][0]
    row = ws.conn.execute("SELECT fan_id, ts FROM test_run WHERE run_id = ?", (run_id,)).fetchone()
    if row is None or row["fan_id"] != fan_id:
        recent = ", ".join(f"{r} ({t[:10]})" for r, t in run_ids(ws, fan_id))
        raise TbfmonError(f"run {run_id} is not a run of {fan_id}", f"recent runs of {fan_id}: {recent}")
    run_day = dt.date.fromisoformat(row["ts"][:10])
    a = ws.assessment(run_day)  # point-in-time: data up to that run's day
    runs = a.data.runs
    idx = runs.index[runs["run_id"] == run_id]
    if not len(idx):
        raise TbfmonError(f"run {run_id} could not be assessed")
    res = _build(ws, a, int(idx[0]))
    res.is_latest_run = run_id == run_ids(ws, fan_id, 1)[0][0]  # latest in the database, not just then
    return res


def _build(ws: Workspace, a: Assessment, i: int) -> FanAnalysis:
    t = a.table.loc[i]
    raw = a.data.runs.loc[i]
    fan_id = t["fan_id"]
    fan = a.data.fan(fan_id)
    ambient = _num(raw["ambient_temp_c"])
    by_param = {r.parameter: r for r in a.limit_results[i]}
    suspects = a.suspects.get(i, {})
    params: list[ParamRow] = []
    for p, m in MEASUREMENTS.items():
        lr = by_param.get(p)
        measured = _num(raw[p])
        if lr is None:
            status = "SUSPECT" if p in suspects else ("no limit" if measured is not None else "not measured")
            params.append(ParamRow(p, m.label, m.unit, measured, None, None, None, None, None, status))
            continue
        status = lr.status if measured is not None else "not measured"
        params.append(ParamRow(
            p, m.label, m.unit, measured,
            _to_measured_units(lr.warn, lr.basis, p, fan, ambient),
            _to_measured_units(lr.alarm, lr.basis, p, fan, ambient),
            lr.side, lr.basis, lr.pct_deviation if measured is not None and status != "SUSPECT" else None, status))
    missing = [g for g, ps in SENSOR_GROUPS.items() if all(_num(raw[p]) is None for p in ps)]
    notes = []
    if suspects:
        notes.append("suspect values excluded from the verdict (re-measure): "
                     + ", ".join(f"{p} ({k})" for p, k in suspects.items()))
    if not bool(t["baseline_established"]):
        notes.append("fewer than 3 comparable runs since install/repair: baseline not established, verdict relies on limits")
    if "vibration (portable analyser)" in missing:
        notes.append("no vibration data on this run — bearing/imbalance/misalignment faults cannot be assessed")
    rules = {m.mode: m for m in a.rules[i]}
    modes = [{"mode": m, "confidence": c, "location": loc,
              "reasons": rules[m].reasons if m in rules else ["model evidence only"]}
             for m, c, loc in a.combined[i]]
    h = a.health[i]
    fan_runs = a.table[a.table["fan_id"] == fan_id]
    rec = recommendation_for(a, i, ws)
    return FanAnalysis(
        fan_id=fan_id, station=fan["station"], capacity_class=fan["capacity_class"],
        run_id=int(raw["run_id"]), run_ts=t["ts"].isoformat(), run_type=str(t["run_type"]),
        duration_min=float(t["duration_min"]), health_index=h.index, status=h.status,
        components=h.components, reasons=h.reasons, params=params, missing_sensors=missing,
        fault_modes=modes, anomaly_percentile=_num(t["anomaly_pct"]), recommendation=rec.to_dict(),
        is_latest_run=int(fan_runs.index[-1]) == i, exit_code=STATUS_EXIT[h.status], notes=notes)


def analyze_latest(ws: Workspace, fan_ids: list[str], as_of: dt.date | str | None = None) -> list[FanAnalysis]:
    """Latest run of each fan from one shared assessment (used by drill and watch)."""
    a = ws.assessment(as_of)
    out = []
    for f in fan_ids:
        i = a.latest_index(f)
        if i is not None:
            out.append(_build(ws, a, i))
    return out


# ---------------------------------------------------------------------- fleet health table


@dataclass
class FleetRow:
    fan_id: str
    station: str
    capacity_class: str
    rated_power_kw: float
    last_run: str | None
    health_index: float | None
    status: str
    top_mode: str | None
    top_confidence: float
    action: str
    hi_history: list[float]
    vib_history: list[float | None]
    temp_history: list[float | None]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


STATUS_ORDER = {"Critical": 0, "Degraded": 1, "Watch": 2, "Healthy": 3, "No data": 4}


def fleet_health(ws: Workspace, station: str | None = None, capacity: str | None = None,
                 status: str | None = None, history: int = 12, as_of: str | None = None) -> list[FleetRow]:
    a = ws.assessment(as_of)
    st = ws.station(station) if station else None
    if capacity and capacity.upper() not in set(a.data.fans["capacity_class"]):
        raise TbfmonError(f"unknown capacity class {capacity!r}",
                          f"classes: {', '.join(sorted(set(a.data.fans['capacity_class'])))}")
    want = status.capitalize() if status else None
    if want and want not in STATUS_ORDER:
        raise TbfmonError(f"unknown status {status!r}", "use healthy, watch, degraded or critical")
    rows = []
    for fan_id, fan in a.data.fans.iterrows():
        if st and fan["station"] != st:
            continue
        if capacity and fan["capacity_class"] != capacity.upper():
            continue
        i = a.latest_index(fan_id)
        hist = a.table[a.table["fan_id"] == fan_id].tail(history)
        if i is None:
            row = FleetRow(fan_id, fan["station"], fan["capacity_class"], fan["rated_power_kw"], None, None,
                           "No data", None, 0.0, "no action", [], [], [])
        else:
            rec = recommendation_for(a, i, ws)
            comb = a.combined[i]
            row = FleetRow(fan_id, fan["station"], fan["capacity_class"], float(fan["rated_power_kw"]),
                           a.table.loc[i, "ts"].isoformat(), a.health[i].index, a.health[i].status,
                           comb[0][0] if comb else None, comb[0][1] if comb else 0.0, rec.action,
                           [float(v) for v in hist["health_index"]],
                           [_num(v) for v in hist["vib_de_rms"]], [_num(v) for v in hist["temp_de_rise"]])
        if want and row.status != want:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (STATUS_ORDER[r.status], r.health_index if r.health_index is not None else 999, r.fan_id))
    return rows


def fleet_exit(rows: list[FleetRow]) -> int:
    return max((STATUS_EXIT.get(r.status, 0) for r in rows), default=0)
