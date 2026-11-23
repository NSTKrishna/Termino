"""Deterministic synthetic data generator. See DESIGN.md §4.

Randomness comes only from seeded numpy Generators with independent streams:
  [seed, 0]        fleet layout            [seed, 1, station]  mock drills
  [seed, 2, fan]   fan baseline            [seed, 3, fan]      fan timeline
  [seed, 4]        data defects (glitches, missing values, duplicates, typos)
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..models import MEASUREMENT_FIELDS, MEASUREMENTS, FailureMode, compute_current_imbalance
from .faults import REPAIR, ROOT_CAUSE, SUDDEN_CAUSES, Effects, Episode, apply, generate_episodes
from .fleet import RATED_RPM, RATED_VOLTAGE, FanSpec, build_fleet, new_bearing

DEFAULT_START = dt.date(2023, 1, 1)

GLITCH_RATE = 0.015
EXTRA_MISSING_RATE = 0.015
DUPLICATE_ROW_RATE = 0.005
FAN_ID_TYPO_RATE = 0.002

# Instrument availability per run (probability a parameter group was measured).
AVAILABILITY: dict[str, tuple[float, tuple[str, ...]]] = {
    "vibration": (0.90, ("vib_de_rms_mms", "vib_de_1x_mms", "vib_de_2x_mms",
                         "vib_nde_rms_mms", "vib_nde_1x_mms", "vib_nde_2x_mms")),
    "bearing_temp": (0.92, ("temp_bearing_de_c", "temp_bearing_nde_c")),
    "winding_temp": (0.85, ("temp_winding_c",)),
    "mcc_meter": (0.95, ("current_l1_a", "current_l2_a", "current_l3_a", "current_imbalance_pct", "voltage_v")),
    "tacho": (0.80, ("rpm",)),
    "aero": (0.45, ("airflow_m3s", "diff_pressure_pa")),
    "noise": (0.40, ("noise_dba",)),
    "megger": (0.35, ("insulation_resistance_mohm",)),
    "scada_start": (0.85, ("runup_time_s", "inrush_peak_a")),
    "scada_stop": (0.70, ("coastdown_time_s",)),
    "ambient": (0.95, ("ambient_temp_c",)),
}
ROUNDING: dict[str, int] = {
    **{m: 2 for m in MEASUREMENT_FIELDS if m.startswith("vib_")},
    "temp_bearing_de_c": 1, "temp_bearing_nde_c": 1, "temp_winding_c": 1,
    "current_l1_a": 1, "current_l2_a": 1, "current_l3_a": 1, "current_imbalance_pct": 2,
    "voltage_v": 0, "rpm": 0, "airflow_m3s": 2, "diff_pressure_pa": 0, "noise_dba": 1,
    "insulation_resistance_mohm": 1, "runup_time_s": 1, "coastdown_time_s": 1,
    "inrush_peak_a": 0, "ambient_temp_c": 1,
}

PM_OK_FINDINGS = ["No abnormality observed", "Checked OK", "Satisfactory",
                  "Minor dust accumulation cleaned", "Checked OK; guards secure"]

BUNDLE_FILES = {
    "fans": "fans.csv",
    "pm_schedule": "pm_schedule.csv",
    "test_runs": "test_runs.csv",
    "maintenance": "maintenance.csv",
    "failures": "failures.csv",
}
GROUND_TRUTH_FILE = "ground_truth.csv"


@dataclass
class Dataset:
    fans: pd.DataFrame
    pm_schedule: pd.DataFrame
    test_runs: pd.DataFrame
    maintenance: pd.DataFrame
    failures: pd.DataFrame
    ground_truth: pd.DataFrame  # kept separate; never ingested
    params: dict = field(default_factory=dict)

    def write(self, sim_dir: Path, ground_truth_dir: Path) -> dict[str, Path]:
        sim_dir.mkdir(parents=True, exist_ok=True)
        ground_truth_dir.mkdir(parents=True, exist_ok=True)
        out: dict[str, Path] = {}
        for attr, name in BUNDLE_FILES.items():
            out[attr] = sim_dir / name
            getattr(self, attr).to_csv(out[attr], index=False, lineterminator="\n")
        out["ground_truth"] = ground_truth_dir / GROUND_TRUTH_FILE
        self.ground_truth.to_csv(out["ground_truth"], index=False, lineterminator="\n")
        return out

    def summary(self) -> dict:
        gt = self.ground_truth
        return {
            **self.params,
            "fans": len(self.fans),
            "test_runs": len(self.test_runs),
            "maintenance_records": len(self.maintenance),
            "failures": len(self.failures),
            "fault_episodes": len(gt),
            "episode_outcomes": gt["outcome"].value_counts().sort_index().to_dict() if len(gt) else {},
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def add_years(d: dt.date, years: int) -> dt.date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + years, day=28)


def _night_time(rng: np.random.Generator, d: dt.date) -> dt.datetime:
    # Non-revenue hours 00:30–04:30.
    minutes = 30 + int(rng.integers(0, 240))
    return dt.datetime.combine(d, dt.time(minutes // 60, minutes % 60))


def _poisson_dates(rng: np.random.Generator, rate_per_year: float, start: dt.date, end: dt.date) -> list[dt.date]:
    days = (end - start).days
    n = int(rng.poisson(rate_per_year * days / 365.25))
    return sorted(start + dt.timedelta(days=int(x)) for x in rng.integers(0, days, n))


def tunnel_ambient(d: dt.date) -> float:
    doy = d.timetuple().tm_yday
    return 30.0 + 6.0 * math.sin(2 * math.pi * (doy - 105) / 365.0)


def humidity_ir_factor(d: dt.date) -> float:
    doy = d.timetuple().tm_yday  # monsoon (Jul–Sep) depresses IR
    return 1.0 - 0.35 * math.exp(-(((doy - 220) / 40.0) ** 2))


def _nan() -> float:
    return float("nan")


# --------------------------------------------------------------------------
# Readings
# --------------------------------------------------------------------------


def true_readings(fan: FanSpec, ts: dt.datetime, duration_min: float, effects: Effects,
                  rng: np.random.Generator) -> dict[str, float]:
    bl, spec = fan.baseline, fan.spec
    amb = tunnel_ambient(ts.date()) + rng.normal(0, 1.2)
    out: dict[str, float] = {"ambient_temp_c": amb}

    jitter = 0.08 * effects.scatter
    for pos in ("DE", "NDE"):
        c1 = (bl.c1[pos] + effects.c1[pos]) * math.exp(rng.normal(0, jitter))
        c2 = (bl.c2[pos] + effects.c2[pos]) * math.exp(rng.normal(0, jitter))
        bb = (bl.bb[pos] + effects.bb[pos]) * math.exp(rng.normal(0, jitter))
        p = pos.lower()
        out[f"vib_{p}_1x_mms"] = c1
        out[f"vib_{p}_2x_mms"] = c2
        out[f"vib_{p}_rms_mms"] = math.sqrt(c1**2 + c2**2 + bb**2)

    fb = 1 - math.exp(-duration_min / bl.tau_bearing_min)
    fw = 1 - math.exp(-duration_min / bl.tau_winding_min)
    for pos in ("DE", "NDE"):
        rise = (bl.rise_bearing[pos] + effects.temp[pos]) * fb
        out[f"temp_bearing_{pos.lower()}_c"] = amb + rise + rng.normal(0, 0.8)

    load = bl.load_factor * effects.current_mult
    out["temp_winding_c"] = amb + (bl.rise_winding * load / 0.85 + effects.winding) * fw + rng.normal(0, 1.5)

    voltage = RATED_VOLTAGE * (1 + rng.normal(0, 0.015))
    current = spec.current_a * load * (RATED_VOLTAGE / voltage) ** 0.5 * (1 + rng.normal(0, 0.01))
    # Fault-driven imbalance pushes phase L1 up and L2/L3 down (rotated per fan).
    skew = np.roll(np.array([1.0, -0.5, -0.5]), fan.index % 3) * effects.imbalance_pct / 100.0
    phases = [current * (1 + a + s + rng.normal(0, 0.004)) for a, s in zip(bl.phase_asym, skew)]
    out["current_l1_a"], out["current_l2_a"], out["current_l3_a"] = phases
    out["voltage_v"] = voltage

    rpm = 1500.0 - 20.0 * load / 0.85 + rng.normal(0, 1.5)
    out["rpm"] = rpm
    speed = rpm / RATED_RPM
    out["airflow_m3s"] = spec.airflow_m3s * bl.airflow_factor * effects.airflow_mult * speed * (1 + rng.normal(0, 0.03))
    out["diff_pressure_pa"] = spec.diff_pressure_pa * bl.dp_factor * effects.dp_mult * speed**2 * (1 + rng.normal(0, 0.03))
    out["noise_dba"] = bl.noise_dba + effects.noise + rng.normal(0, 0.8)

    # IR roughly halves per +10 °C winding temperature (measured cold, at ambient).
    ir = bl.ir_mohm * humidity_ir_factor(ts.date()) * effects.ir_mult * 0.5 ** ((amb - 30.0) / 10.0)
    out["insulation_resistance_mohm"] = ir * math.exp(rng.normal(0, 0.12))

    out["runup_time_s"] = bl.runup_s * effects.runup_mult * (1 + rng.normal(0, 0.04))
    out["coastdown_time_s"] = bl.coastdown_s * effects.coast_mult * (1 + rng.normal(0, 0.04))
    out["inrush_peak_a"] = spec.current_a * bl.inrush_mult * (voltage / RATED_VOLTAGE) * (1 + rng.normal(0, 0.03))
    return out


def observe(true: dict[str, float], run_type: str, rng: np.random.Generator,
            trip: str | None = None) -> dict[str, float]:
    """Apply instrument availability; round like the instruments/log sheets do."""
    obs = {m: _nan() for m in MEASUREMENT_FIELDS}
    for group, (p, fields) in AVAILABILITY.items():
        if run_type == "commissioning" and group in ("aero", "megger", "noise", "vibration"):
            p = 1.0
        elif run_type == "mock_drill" and group == "aero":
            p = 0.6
        if trip == "no_start":
            p = p if group in ("mcc_meter", "ambient") else 0.0
        elif trip == "earth_fault":
            p = 1.0 if group in ("megger", "ambient") else (p if group == "scada_start" else 0.0)
        elif trip == "mechanical":
            p = 0.0 if group in ("aero", "scada_stop") else p
        if rng.random() < p:
            for f in fields:
                if f in true:
                    obs[f] = true[f]
    if trip == "no_start":
        for f in ("current_l1_a", "current_l2_a", "current_l3_a"):
            obs[f] = _nan()
    obs["current_imbalance_pct"] = _nan()
    imb = compute_current_imbalance(*(None if math.isnan(obs[f]) else obs[f]
                                      for f in ("current_l1_a", "current_l2_a", "current_l3_a")))
    if imb is not None:
        obs["current_imbalance_pct"] = imb
    return {k: (v if math.isnan(v) else round(float(v), ROUNDING[k])) for k, v in obs.items()}


# --------------------------------------------------------------------------
# Symptoms for failure records (from the fan's own pre-failure observations)
# --------------------------------------------------------------------------

REL_THRESHOLD = {"vib": 0.30, "temp": 0.20, "insulation_resistance_mohm": 0.40, "airflow_m3s": 0.08,
                 "diff_pressure_pa": 0.08, "current": 0.03, "coastdown_time_s": 0.08}
ABS_THRESHOLD = {"noise_dba": 2.0, "current_imbalance_pct": 1.5}


def _val(run: dict, p: str) -> float | None:
    v = run.get(p)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if p.startswith("temp_"):
        amb = run.get("ambient_temp_c")
        if amb is None or math.isnan(amb):
            return None
        return v - amb
    return v


def _threshold(p: str) -> tuple[str, float]:
    if p in ABS_THRESHOLD:
        return "abs", ABS_THRESHOLD[p]
    for prefix in ("vib", "temp", "current"):
        if p.startswith(prefix):
            return "rel", REL_THRESHOLD[prefix]
    return "rel", REL_THRESHOLD[p]


def compute_symptoms(ep: Episode, history: list[dict], fail_run: dict | None, d: dt.date) -> list[dict]:
    if ep.sudden:
        return []
    ref_runs = [r for r in history if d - dt.timedelta(days=540) <= r["ts"].date() <= d - dt.timedelta(days=150)]
    early_runs = [r for r in history if r["ts"].date() < d - dt.timedelta(days=60)][:3]
    recent_runs = [r for r in history if d - dt.timedelta(days=120) < r["ts"].date() < d]
    symptoms = []
    for p, direction in ep.signature():
        unit = "K rise" if p.startswith("temp_") else MEASUREMENTS[p].unit
        refs = [v for r in ref_runs if (v := _val(r, p)) is not None]
        if len(refs) < 2:
            refs = [v for r in early_runs if (v := _val(r, p)) is not None]
        recent = [v for r in recent_runs if (v := _val(r, p)) is not None][-2:]
        source = f"{len(recent)} run(s) in the 120 days before failure"
        if not recent and fail_run is not None and not p.startswith("temp_") and _val(fail_run, p) is not None:
            recent, source = [_val(fail_run, p)], "failure run"
        if not refs or not recent:
            continue
        ref, cur = float(np.median(refs)), float(np.mean(recent))
        kind, thr = _threshold(p)
        delta = cur - ref
        change = delta if kind == "abs" else (delta / ref if ref else 0.0)
        moved = change >= thr if direction == "increase" else change <= -thr
        if moved:
            pct = f"{delta:+.1f} {unit}" if kind == "abs" else f"{change * 100:+.0f}%"
            symptoms.append({"parameter": p, "direction": direction,
                             "note": f"{ref:.3g}→{cur:.3g} {unit} ({pct}) from {source}"})
    if ep.mode is FailureMode.MISALIGNMENT_LOOSENESS:
        vals = [v for r in recent_runs[-3:] if (v := _val(r, "vib_de_rms_mms")) is not None]
        if len(vals) >= 2 and np.std(vals) / np.mean(vals) > 0.25:
            symptoms.append({"parameter": "vib_de_rms_mms", "direction": "erratic",
                             "note": f"run-to-run scatter {np.std(vals) / np.mean(vals) * 100:.0f}% (CV)"})
    if not symptoms:
        p, direction = ep.signature()[0]
        symptoms.append({"parameter": p, "direction": direction,
                         "note": "reported by maintainer at failure; no usable pre-failure run data"})
    return symptoms


def encode_symptoms(symptoms: list[dict]) -> str:
    return "|".join(f"{s['parameter']}:{s['direction']}:{s['note']}" for s in symptoms)


# --------------------------------------------------------------------------
# PM plan and findings
# --------------------------------------------------------------------------


def pm_plan(fan: FanSpec, rng: np.random.Generator, start: dt.date, end: dt.date) -> list[tuple[dt.datetime, str, bool]]:
    events = []
    for task, months, _hours, height in fan.pm_schedule:
        period = months * 30.44
        compliance = 0.5 if "replacement" in task else (0.78 if height else 0.9)
        last = start - dt.timedelta(days=float(rng.uniform(0, period)))
        due = last + dt.timedelta(days=period)
        while due < end:
            if rng.random() < compliance:
                delay = min(float(rng.exponential(12.0)), 60.0)
                actual = due + dt.timedelta(days=delay)
                if actual < end:
                    events.append((_night_time(rng, actual), task, height))
                last = actual
            else:
                last = due
            due = last + dt.timedelta(days=period)
    return events


def pm_effect(task: str, ep: Episode, s: float, rng: np.random.Generator) -> tuple[bool, str | None]:
    """(resolves_episode, finding_text) for a PM task performed while `ep` is active."""
    t = task.lower()
    m = ep.mode
    L = ep.location or "DE"
    if s >= 1.0 or ep.sudden:
        return False, None
    if "regreas" in t:
        if m is FailureMode.LUBRICATION_LOSS:
            return rng.random() < 0.85, f"{L} bearing grease dry and discoloured; purged and regreased"
        if m is FailureMode.BEARING_WEAR and s > 0.35:
            return False, f"Metallic particles in purged {L} grease; bearing rumble on hand rotation"
    if "cleaning" in t:
        if m is FailureMode.AIRFLOW_RESTRICTION:
            return rng.random() < 0.8, "Silencer and guard partially clogged with debris; cleaned"
        if m is FailureMode.IMPELLER_IMBALANCE:
            return rng.random() < 0.3, "Heavy dust deposit on impeller blades; cleaned"
        if m is FailureMode.MISALIGNMENT_LOOSENESS and s > 0.4:
            return False, "Guard bolts found loose; tightened"
    if "torque" in t:
        if m is FailureMode.MISALIGNMENT_LOOSENESS:
            return rng.random() < 0.65, "Mounting bolts below torque; re-torqued, AV mounts cracked"
    if "balance" in t and m is FailureMode.IMPELLER_IMBALANCE:
        return rng.random() < 0.85, "Residual unbalance above G6.3; trim-balanced in situ"
    if "bearing replacement" in t and m in (FailureMode.BEARING_WEAR, FailureMode.LUBRICATION_LOSS):
        return True, f"{L} bearing showed wear on replacement"
    if "insulation" in t and m is FailureMode.INSULATION_DEGRADATION and s > 0.4:
        return False, "IR low and falling vs previous test; recommend monitoring / drying"
    return False, None


# --------------------------------------------------------------------------
# Main generator
# --------------------------------------------------------------------------


def generate(n_fans: int = 40, years: int = 3, seed: int = 42, start: dt.date = DEFAULT_START) -> Dataset:
    if n_fans < 1 or years < 1:
        raise ValueError("fans and years must be >= 1")
    end = add_years(start, years)
    fleet = build_fleet(n_fans, seed)

    stations = list(dict.fromkeys(f.station for f in fleet))
    drills: dict[str, list[dt.datetime]] = {}
    for si, st in enumerate(stations):
        srng = np.random.default_rng([seed, 1, si])
        rate = float(srng.uniform(1.0, 3.0))  # station-wide mock drills per year
        drills[st] = [_night_time(srng, d) for d in _poisson_dates(srng, rate, start, end)]

    fan_rows, pm_rows, run_rows, mnt_rows, fail_rows, gt_rows = [], [], [], [], [], []
    counter = [0]
    for fan in fleet:
        rng = np.random.default_rng([seed, 3, fan.index])
        fan_rows.append(fan.row())
        pm_rows += [{"fan_id": fan.fan_id, "task": t, "interval_months": m, "interval_run_hours": h,
                     "needs_work_at_height": w} for t, m, h, w in fan.pm_schedule]
        episodes = generate_episodes(rng, fan.fan_id, fan.install_date, start, end, counter)
        runs, mnts, fails = _simulate_fan(fan, episodes, drills[fan.station],
                                          rng, start, end, stations_pos=_station_position(fleet, fan))
        run_rows += runs
        mnt_rows += mnts
        fail_rows += fails
        for ep in episodes:
            gt_rows.append({
                "fan_id": ep.fan_id, "fault_id": ep.fault_id, "fault_mode": ep.mode.value,
                "location": ep.location or "", "onset_date": ep.onset.isoformat(),
                "failure_date": ep.failure_date.isoformat() if ep.failure_date else "",
                "end_date": ep.end_date.isoformat() if ep.end_date else "",
                "outcome": ep.outcome,
            })

    runs_df = pd.DataFrame(run_rows, columns=["fan_id", "timestamp", "run_type", "duration_min", *MEASUREMENT_FIELDS])
    runs_df = _inject_defects(runs_df, np.random.default_rng([seed, 4]))

    pm_df = pd.DataFrame(pm_rows)
    pm_df["interval_months"] = pm_df["interval_months"].astype("Int64")
    return Dataset(
        fans=pd.DataFrame(fan_rows),
        pm_schedule=pm_df,
        test_runs=runs_df,
        maintenance=pd.DataFrame(mnt_rows, columns=["fan_id", "date", "task", "type", "parts_replaced",
                                                    "findings", "man_hours", "work_at_height"])
        .sort_values(["fan_id", "date", "task"], kind="stable").reset_index(drop=True),
        failures=pd.DataFrame(fail_rows, columns=["fan_id", "date", "failure_mode", "symptoms",
                                                  "root_cause", "fix", "downtime_hours"]),
        ground_truth=pd.DataFrame(gt_rows, columns=["fan_id", "fault_id", "fault_mode", "location", "onset_date",
                                                    "failure_date", "end_date", "outcome"]),
        params={"seed": seed, "fans_requested": n_fans, "years": years,
                "start": start.isoformat(), "end": end.isoformat()},
    )


def _station_position(fleet: list[FanSpec], fan: FanSpec) -> int:
    return [f.fan_id for f in fleet if f.station == fan.station].index(fan.fan_id)


def _simulate_fan(fan: FanSpec, episodes: list[Episode], station_drills: list[dt.datetime],
                  rng: np.random.Generator, start: dt.date, end: dt.date, stations_pos: int):
    # ---- plan runs: station mock drills + per-fan scheduled tests (≈4–12/yr total)
    days = (end - start).days
    target = int(round(float(rng.uniform(4.0, 12.0)) * days / 365.25))
    planned: dict[dt.date, tuple[dt.datetime, str]] = {}
    for ts in station_drills:
        planned[ts.date()] = (ts + dt.timedelta(minutes=15 * stations_pos), "mock_drill")
    free_days = [d for x in rng.permutation(days) if (d := start + dt.timedelta(days=int(x))) not in planned]
    for d in free_days[: max(0, target - len(planned))]:  # irregular: random distinct nights
        planned[d] = (_night_time(rng, d), "scheduled_test")

    # Event queue: (datetime, seq, kind, payload)
    queue: list[tuple[dt.datetime, int, str, object]] = []
    seq = [0]

    def push(ts: dt.datetime, kind: str, payload: object) -> None:
        seq[0] += 1
        bisect.insort(queue, (ts, seq[0], kind, payload), key=lambda e: (e[0], e[1]))

    for ts, run_type in planned.values():
        push(ts, "run", run_type)
    for ts, task, height in pm_plan(fan, rng, start, end):
        push(ts, "pm", (task, height))

    runs: list[dict] = []
    history: list[dict] = []
    mnts: list[dict] = []
    fails: list[dict] = []
    out_of_service_until: dt.date | None = None

    i = 0
    while i < len(queue):
        ts, _, kind, payload = queue[i]
        i += 1
        d = ts.date()
        if d >= end:
            break
        if out_of_service_until and d < out_of_service_until:
            continue  # awaiting repair: drills/tests/PM skip this fan
        active = [(e, e.severity(d)) for e in episodes if e.active(d)]

        if kind == "pm":
            task, height = payload
            findings = []
            for ep, s in active:
                resolves, text = pm_effect(task, ep, s, rng)
                if text:
                    findings.append(text)
                if resolves:
                    ep.end_date, ep.outcome = d, "resolved_by_pm"
                    _reset_component(fan, ep, rng)
            if "insulation" in task.lower():
                ir = true_readings(fan, ts, 1.0, _effects(active), rng)["insulation_resistance_mohm"]
                findings.append(f"IR {ir:.0f} MΩ @500 V DC")
            parts = ["grease cartridge"] if "regreas" in task.lower() else (
                ["DE bearing 6314-C3", "NDE bearing 6312-C3", "grease LGHP2"] if "bearing replacement" in task.lower() else [])
            mnts.append({
                "fan_id": fan.fan_id, "date": d.isoformat(), "task": task, "type": "preventive",
                "parts_replaced": ";".join(parts),
                "findings": "; ".join(findings) if findings else str(rng.choice(PM_OK_FINDINGS)),
                "man_hours": round(float(rng.uniform(6, 16) if height else rng.uniform(1, 3)), 1),
                "work_at_height": height,
            })
            continue

        run_type = payload
        failing = [(e, s) for e, s in active if s >= 1.0]
        if failing:
            ep = min(failing, key=lambda x: x[0].planned_failure)[0]
            trip = ("no_start" if ep.sudden else
                    "earth_fault" if ep.mode is FailureMode.INSULATION_DEGRADATION else "mechanical")
            duration = {"no_start": 0.1, "earth_fault": float(rng.uniform(0.2, 0.5)),
                        "mechanical": float(rng.uniform(1.0, 4.0))}[trip]
            true = true_readings(fan, ts, duration, _effects(active), rng)
            obs = observe(true, run_type, rng, trip=trip)
            fail_run = {"ts": ts, **obs}
            runs.append(_run_row(fan, ts, run_type, duration, obs))
            symptoms = compute_symptoms(ep, history, fail_run, d)
            history.append(fail_run)

            repair_d = d + dt.timedelta(days=int(rng.integers(1, 11)))
            L = ep.location or "DE"
            if ep.sudden:
                cause, fix = SUDDEN_CAUSES[int(rng.integers(0, len(SUDDEN_CAUSES)))]
                task, parts = "Control circuit repair", [fix.split(" ", 1)[1]]
            else:
                task, parts, fix = REPAIR[ep.mode]
                task, fix = task.format(L=L), fix.format(L=L)
                parts = [p.format(L=L) for p in parts]
                cause = ROOT_CAUSE[ep.mode].format(L=L)
            repair_hours = float(rng.uniform(6, 30))
            fails.append({
                "fan_id": fan.fan_id, "date": d.isoformat(), "failure_mode": ep.mode.value,
                "symptoms": encode_symptoms(symptoms), "root_cause": cause, "fix": fix,
                "downtime_hours": round((repair_d - d).days * 24 + repair_hours, 1),
            })
            mnts.append({
                "fan_id": fan.fan_id, "date": repair_d.isoformat(), "task": task, "type": "corrective",
                "parts_replaced": ";".join(parts), "findings": f"Found: {cause}",
                "man_hours": round(float(rng.uniform(8, 24)), 1), "work_at_height": not ep.sudden,
            })
            ep.failure_date, ep.end_date, ep.outcome = d, repair_d, "failed"
            for other, _ in active:  # the repair also fixes co-located bearing faults
                if other is not ep and other.location == ep.location and other.mode in (
                        FailureMode.BEARING_WEAR, FailureMode.LUBRICATION_LOSS) and ep.mode in (
                        FailureMode.BEARING_WEAR, FailureMode.LUBRICATION_LOSS):
                    other.end_date, other.outcome = repair_d, "resolved_by_pm"
            _reset_component(fan, ep, rng)
            out_of_service_until = repair_d
            push(_night_time(rng, repair_d), "run", "commissioning")
            continue

        if run_type == "mock_drill":
            duration = float(rng.uniform(20, 60))
        elif run_type == "commissioning":
            duration = float(rng.uniform(60, 120))
        else:
            duration = float(rng.uniform(5, 30))
        true = true_readings(fan, ts, duration, _effects(active), rng)
        obs = observe(true, run_type, rng)
        runs.append(_run_row(fan, ts, run_type, duration, obs))
        history.append({"ts": ts, **obs})
    return runs, mnts, fails


def _effects(active: list[tuple[Episode, float]]) -> Effects:
    eff = Effects()
    for ep, s in active:
        apply(eff, ep, s)
    return eff


def _reset_component(fan: FanSpec, ep: Episode, rng: np.random.Generator) -> None:
    """After a repair the replaced component gets a fresh (healthy) baseline."""
    bl = fan.baseline
    m = ep.mode
    if m in (FailureMode.BEARING_WEAR, FailureMode.LUBRICATION_LOSS):
        pos = ep.location or "DE"
        bl.c1[pos], bl.c2[pos], bl.bb[pos] = new_bearing(rng, pos)
    elif m is FailureMode.IMPELLER_IMBALANCE:
        bl.c1["DE"] = new_bearing(rng, "DE")[0]
        bl.c1["NDE"] = new_bearing(rng, "NDE")[0]
    elif m is FailureMode.MISALIGNMENT_LOOSENESS:
        bl.c2["DE"] = new_bearing(rng, "DE")[1]
    elif m is FailureMode.INSULATION_DEGRADATION and ep.outcome == "failed":
        bl.ir_mohm = float(np.clip(1500.0 * math.exp(rng.normal(0.3, 0.3)), 500.0, 6000.0))


def _run_row(fan: FanSpec, ts: dt.datetime, run_type: str, duration: float, obs: dict) -> dict:
    return {"fan_id": fan.fan_id, "timestamp": ts.isoformat(), "run_type": run_type,
            "duration_min": round(duration, 1), **obs}


# --------------------------------------------------------------------------
# Data defects
# --------------------------------------------------------------------------

TEMP_FIELDS = ("temp_bearing_de_c", "temp_bearing_nde_c", "temp_winding_c", "ambient_temp_c")


def _inject_defects(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    cols = list(MEASUREMENT_FIELDS)
    values = df[cols].to_numpy(dtype=float, copy=True)
    present = ~np.isnan(values)

    glitch = (rng.random(values.shape) < GLITCH_RATE) & present
    kinds = rng.choice(["spike", "zero", "sentinel", "fahrenheit", "decimal"], size=values.shape,
                       p=[0.35, 0.2, 0.15, 0.15, 0.15])
    factors = rng.uniform(3.0, 10.0, values.shape)
    sentinel = np.where(rng.random(values.shape) < 0.5, -999.0, 9999.0)
    decimal = np.where(rng.random(values.shape) < 0.5, 10.0, 0.1)
    for r, c in zip(*np.nonzero(glitch)):
        name, v, k = cols[c], values[r, c], kinds[r, c]
        if k == "fahrenheit" and name not in TEMP_FIELDS:
            k = "spike"
        if k == "spike":
            v = v * factors[r, c]
        elif k == "zero":
            v = 0.0
        elif k == "sentinel":
            v = sentinel[r, c]
        elif k == "fahrenheit":
            v = v * 1.8 + 32.0
        else:
            v = v * decimal[r, c]
        values[r, c] = round(v, ROUNDING[name])

    values[(rng.random(values.shape) < EXTRA_MISSING_RATE) & present] = np.nan
    df[cols] = values

    typo = rng.random(len(df)) < FAN_ID_TYPO_RATE
    df.loc[typo, "fan_id"] = df.loc[typo, "fan_id"].str.replace("TBF-0", "TBF-O", n=1, regex=False)

    dup_idx = np.nonzero(rng.random(len(df)) < DUPLICATE_ROW_RATE)[0]
    df = pd.concat([df, df.iloc[dup_idx]], ignore_index=True)
    return df.sort_values(["fan_id", "timestamp"], kind="stable").reset_index(drop=True)
