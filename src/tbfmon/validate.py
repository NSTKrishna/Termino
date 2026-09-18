"""Data-quality and consistency checks (`tbfmon validate`). See DESIGN.md §6–7.

Checks never modify data. Suspect values are *flagged*, not removed: a
persistent deviation is degradation, only an isolated one is a likely glitch.
"""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import ValidationError

from . import db
from .limits import load_limits
from .models import MEASUREMENT_FIELDS, MEASUREMENTS

GENERIC_TAU_MIN = 25.0  # duration normalisation for temperatures (DESIGN.md §7.5)
SPIKE_K = 5.0
# (absolute floor, relative floor) of the neighbour scale per parameter family.
SCALE_FLOORS: dict[str, tuple[float, float]] = {
    "vib": (0.3, 0.15), "temp": (5.0, 0.20), "current_l": (2.0, 0.05), "current_imbalance_pct": (1.0, 0.30),
    "voltage_v": (8.0, 0.02), "rpm": (10.0, 0.005), "airflow_m3s": (1.0, 0.06), "diff_pressure_pa": (30.0, 0.06),
    "noise_dba": (2.0, 0.0), "insulation_resistance_mohm": (0.15, 0.0), "runup_time_s": (1.5, 0.10),
    "coastdown_time_s": (8.0, 0.08), "inrush_peak_a": (30.0, 0.08), "ambient_temp_c": (3.0, 0.0),
}
PHASES = ("current_l1_a", "current_l2_a", "current_l3_a")
PHASE_TOLERANCE = 0.30
NONZERO_WHEN_RUNNING = [m for m in MEASUREMENT_FIELDS
                        if m.startswith(("vib_", "temp_", "current_l", "voltage", "rpm", "airflow", "diff_", "inrush"))]


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL
    count: int
    detail: str
    examples: list[dict[str, Any]] = field(default_factory=list)


def _floors(p: str) -> tuple[float, float]:
    for key, v in SCALE_FLOORS.items():
        if p.startswith(key):
            return v
    return (0.5, 0.1)


def _series_for(df: pd.DataFrame, p: str) -> pd.Series:
    """Values in the domain used for spike detection."""
    if p.startswith("temp_"):
        frac = 1 - np.exp(-df["duration_min"] / GENERIC_TAU_MIN)
        return (df[p] - df["ambient_temp_c"]) / frac  # duration-normalised rise over ambient
    if p == "insulation_resistance_mohm":
        return np.log10(df[p].where(df[p] > 0))
    return df[p]


def suspect_points(g: pd.DataFrame, p: str, k: float = SPIKE_K,
                   repairs: list[str] | None = None) -> dict[Any, dict[str, Any]]:
    """Suspect values of parameter `p` in one fan's runs (sorted by time).

    * isolated    — deviates from both neighbouring runs while they agree (two-sided test)
    * unconfirmed — newest value deviates sharply from the previous runs (one-sided;
                    may be a glitch *or* a sudden real change: re-measure to confirm)
    * inconsistent — a phase current far from the other two phases in the same run
    * stuck_zero  — exactly zero where a running fan cannot read zero
    `repairs` (ISO dates of corrective maintenance) split the history: a repair
    legitimately changes the baseline, so runs are only compared within a segment.
    Returns {row index: {"kind", "ref"}} with `ref` in the parameter's own unit.
    """
    if repairs:
        day = g["ts"].dt.strftime("%Y-%m-%d")
        segment = np.searchsorted(np.array(sorted(repairs)), day.to_numpy(), side="right")
        out: dict[Any, dict[str, Any]] = {}
        for _, part in g.groupby(segment, sort=True):
            out.update(suspect_points(part, p, k))
        return out
    s = _series_for(g, p).dropna()
    vals, idx = s.to_numpy(), s.index.to_numpy()
    absf, relf = _floors(p)
    out: dict[Any, dict[str, Any]] = {}
    if p in NONZERO_WHEN_RUNNING:
        for i in g.index[g[p] == 0]:
            out[i] = {"kind": "stuck_zero", "ref": None}
    if p in PHASES:
        # Same-run physics: one phase far from the other two is implausible on a running motor.
        ph = g[list(PHASES)].dropna()
        med = ph.median(axis=1)
        bad = (ph[p] - med).abs() > PHASE_TOLERANCE * med
        for i in ph.index[bad]:
            out.setdefault(i, {"kind": "inconsistent", "ref": round(float(med[i]), 3)})
    for i in range(1, len(vals)):
        last = i == len(vals) - 1
        left = vals[max(0, i - (3 if last else 2)):i]
        right = vals[i + 1:i + 3]
        if last and len(left) < 2:
            continue
        nb = np.concatenate([left, right])
        m = float(np.median(nb))
        scale = max(1.4826 * float(np.median(np.abs(nb - m))), absf, relf * abs(m))
        # Must disagree with each side separately: a step confirmed by the next run is real.
        if idx[i] not in out and all(abs(vals[i] - float(np.median(side))) > k * scale
                                     for side in (left, right) if len(side)):
            out[idx[i]] = {"kind": "unconfirmed" if last else "isolated", "ref": _back(p, m, g, idx[i])}
    return out


def repair_dates(maint: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in maint:
        if m["type"] == "corrective":
            out.setdefault(m["fan_id"], []).append(m["date"])
    return out


def find_isolated_spikes(runs: pd.DataFrame, repairs: dict[str, list[str]] | None = None,
                         k: float = SPIKE_K) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for fan_id, g in runs.sort_values("ts").groupby("fan_id", sort=True):
        for p in MEASUREMENT_FIELDS:
            for i, sp in suspect_points(g, p, k, (repairs or {}).get(fan_id)).items():
                if sp["kind"] in ("unconfirmed", "stuck_zero"):
                    continue  # newest value → fleet/fan views; zeros → stuck_zero_values check
                raw = float(g.at[i, p])
                found.append({"fan_id": fan_id, "ts": g.at[i, "ts"].isoformat(), "parameter": p,
                              "value": raw, "neighbour_median": sp["ref"], "hint": _hint(p, raw, sp["ref"])})
    return found


def _back(p: str, m: float, g: pd.DataFrame, i) -> float | None:
    """Convert a neighbour median from detection domain back to the parameter's unit."""
    if p.startswith("temp_"):
        amb, dur = g.at[i, "ambient_temp_c"], g.at[i, "duration_min"]
        return round(float(amb + m * (1 - math.exp(-dur / GENERIC_TAU_MIN))), 2)
    if p == "insulation_resistance_mohm":
        return round(10**m, 2)
    return round(m, 3)


def _hint(p: str, v: float, ref: float | None) -> str:
    if ref is None or ref == 0:
        return "spike"
    if v == 0:
        return "stuck zero"
    if p.startswith("temp_") or p == "ambient_temp_c":
        if abs((v - 32) / 1.8 - ref) <= max(3.0, 0.1 * abs(ref)):
            return "looks like °F"
    ratio = v / ref
    if 8 <= ratio <= 12 or 0.08 <= ratio <= 0.125:
        return "decimal shift ×10"
    return "spike" if ratio > 1 else "dropout"


def _physical(runs: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for r in runs.itertuples(index=False):
        base = {"fan_id": r.fan_id, "ts": r.ts.isoformat()}
        for pos in ("de", "nde"):
            rms, c1, c2 = getattr(r, f"vib_{pos}_rms_mms"), getattr(r, f"vib_{pos}_1x_mms"), getattr(r, f"vib_{pos}_2x_mms")
            comp = np.nanmax([c1, c2]) if not (math.isnan(c1) and math.isnan(c2)) else float("nan")
            if not math.isnan(rms) and not math.isnan(comp) and rms + 0.05 < comp:
                out.append({**base, "rule": f"vib_{pos}: overall RMS {rms} < component {comp}"})
        amb = r.ambient_temp_c
        for p in ("temp_bearing_de_c", "temp_bearing_nde_c", "temp_winding_c"):
            v = getattr(r, p)
            if not math.isnan(v) and not math.isnan(amb) and v < amb - 3:
                out.append({**base, "rule": f"{p} {v} below ambient {amb}"})
        if not math.isnan(r.rpm) and r.rpm > 1500 * 1.005:
            out.append({**base, "rule": f"rpm {r.rpm} above synchronous speed"})
        phases = [r.current_l1_a, r.current_l2_a, r.current_l3_a]
        if not any(math.isnan(x) for x in phases) and not math.isnan(r.current_imbalance_pct):
            mean = sum(phases) / 3
            calc = max(abs(x - mean) for x in phases) / mean * 100 if mean > 0 else float("nan")
            if not math.isnan(calc) and abs(calc - r.current_imbalance_pct) > 1.0:
                out.append({**base, "rule": f"stated imbalance {r.current_imbalance_pct}% vs phases {calc:.2f}%"})
    return out


def run_checks(conn: sqlite3.Connection, limits_path: Path | None, as_of: dt.date,
               max_examples: int = 10) -> tuple[list[Check], dict[str, Any]]:
    checks: list[Check] = []

    def add(name: str, n: int, detail: str, examples=None, fail: bool = False, warn_if_nonzero: bool = True):
        status = "FAIL" if fail and n else ("WARN" if warn_if_nonzero and n else "PASS")
        checks.append(Check(name, status, n, detail, (examples or [])[:max_examples]))

    # 1. limits.yaml
    try:
        limits = load_limits(limits_path)
        ph = limits.placeholder_count()
        add("limits_file", ph, f"{ph} limit(s) still marked placeholder — REPLACE WITH OEM VALUES"
            if ph else "all limits approved")
    except (ValidationError, ValueError, OSError) as e:
        limits = None
        add("limits_file", 1, f"invalid limits file: {e}".splitlines()[0], fail=True)

    fans = db.list_fans(conn)
    runs = db.runs_df(conn)
    maint = db.maintenance(conn)
    fails = db.failures(conn)
    tasks = db.pm_tasks(conn)

    # 2. referential
    if limits is not None:
        missing = sorted({f["capacity_class"] for f in fans} - set(limits.classes))
        add("capacity_classes", len(missing), f"classes missing from limits.yaml: {missing}" if missing
            else "every fan's capacity class has limits", fail=True)
    no_runs = sorted({f["fan_id"] for f in fans} - set(runs["fan_id"]))
    add("fans_without_runs", len(no_runs), "fans with no test runs", [{"fan_id": f} for f in no_runs])
    no_pm = sorted({f["fan_id"] for f in fans} - {t["fan_id"] for t in tasks})
    add("fans_without_pm_schedule", len(no_pm), "fans with no OEM PM schedule", [{"fan_id": f} for f in no_pm])

    # 3. ingest log
    issues = db.issues(conn)
    nulled = [i for i in issues if i["action"] == "nulled"]
    by_issue: dict[str, int] = {}
    for i in nulled:
        by_issue[i["issue"]] = by_issue.get(i["issue"], 0) + 1
    add("ingest_nulled_values", len(nulled),
        "values nulled at ingest: " + (", ".join(f"{k}={v}" for k, v in sorted(by_issue.items())) or "none"),
        [{"key": i["entity_key"], "field": i["field"], "raw": i["raw_value"], "issue": i["issue"]} for i in nulled])
    rejected = [i for i in issues if i["action"] == "rejected"]
    add("ingest_rejected_rows", len(rejected), "rows rejected at ingest (unknown fan, schema error)",
        [{"key": i["entity_key"], "field": i["field"], "raw": i["raw_value"], "issue": i["issue"],
          "source": i["source"]} for i in rejected])

    # 4. temporal
    install = {f["fan_id"]: f["install_date"] for f in fans}
    bad_time = [{"fan_id": r.fan_id, "ts": r.ts.isoformat(), "rule": "before install date"}
                for r in runs.itertuples() if r.ts.date().isoformat() < install.get(r.fan_id, "")]
    bad_time += [{"fan_id": m["fan_id"], "ts": m["date"], "rule": "maintenance before install date"}
                 for m in maint if m["date"] < install.get(m["fan_id"], "")]
    today = dt.date.today()
    bad_time += [{"fan_id": r.fan_id, "ts": r.ts.isoformat(), "rule": "dated in the future"}
                 for r in runs.itertuples() if r.ts.date() > today]
    bad_time += [{"fan_id": m["fan_id"], "ts": m["date"], "rule": "maintenance dated in the future"}
                 for m in maint if m["date"] > today.isoformat()]
    add("temporal_consistency", len(bad_time), "records before install date or in the future", bad_time)

    # 5. value plausibility
    zeros = []
    for p in NONZERO_WHEN_RUNNING:
        hit = runs[runs[p] == 0]
        zeros += [{"fan_id": r.fan_id, "ts": r.ts.isoformat(), "parameter": p} for r in hit.itertuples()]
    add("stuck_zero_values", len(zeros), "zero readings impossible on a running fan", zeros)
    spikes = find_isolated_spikes(runs, repair_dates(maint))
    kinds: dict[str, int] = {}
    for s in spikes:
        kinds[s["hint"]] = kinds.get(s["hint"], 0) + 1
    add("isolated_spikes", len(spikes), "suspect values (deviate from both neighbouring runs): "
        + (", ".join(f"{k}={v}" for k, v in sorted(kinds.items())) or "none"), spikes)
    phys = _physical(runs)
    add("physical_consistency", len(phys), "cross-parameter physics violations", phys)

    # 6. operations
    last_run = runs.groupby("fan_id")["ts"].max()
    stale = [{"fan_id": f, "last_run": ts.date().isoformat(), "days": (as_of - ts.date()).days}
             for f, ts in last_run.items() if (as_of - ts.date()).days > 120]
    add("fans_not_run_recently", len(stale), f"fans not run in the 120 days before {as_of}", stale)
    no_sym = [{"fan_id": f["fan_id"], "date": f["date"], "mode": f["failure_mode"]}
              for f in fails if not f["symptoms"] and f["failure_mode"] != "other"]
    add("failures_without_symptoms", len(no_sym), "failure records with no symptoms recorded", no_sym)

    missing = {p: round(float(runs[p].isna().mean()) * 100, 1) if len(runs) else 100.0 for p in MEASUREMENT_FIELDS}
    extra = {
        "as_of": as_of.isoformat(),
        "counts": db.counts(conn),
        "missing_pct": missing,
        "all_spikes": spikes,
    }
    return checks, extra


def checks_to_dict(checks: list[Check]) -> list[dict[str, Any]]:
    return [asdict(c) for c in checks]


def measurement_label(p: str) -> str:
    m = MEASUREMENTS[p]
    return f"{m.label} [{m.unit}]"


# Public names for reuse by the analytics engine (same rules, one source of truth).
scale_floors = _floors
detection_domain = _series_for
