"""Layer 0 — causal suspect-value screening.

Same rules as `tbfmon validate` (DESIGN.md §7.9) but strictly causal: a run is
judged using only earlier runs, so point-in-time evaluation is honest.

* stuck_zero   — exact zero where a running fan cannot read zero
* inconsistent — one phase current >30 % from the median of the three
* unconfirmed  — jumps vs the previous (accepted) values of the same segment;
                 if the *next* value agrees with it, the jump is accepted as real
                 for all later windows (it stays suspect for its own run).
                 A jump is accepted at once when a physically related parameter in
                 the same run also moved notably (CORROBORATE, e.g. vibration with
                 bearing temperature/noise, IR with current imbalance): instrument
                 glitches hit single values independently, real faults move several.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..models import MEASUREMENT_FIELDS
from ..validate import NONZERO_WHEN_RUNNING, PHASES, detection_domain, scale_floors
from .config import CleaningCfg
from .features import PARAM_GROUP, seg_col


def _scale(values: np.ndarray, p: str) -> tuple[float, float]:
    m = float(np.median(values))
    absf, relf = scale_floors(p)
    return m, max(1.4826 * float(np.median(np.abs(values - m))), absf, relf * abs(m))


def _corroboration() -> dict[str, tuple[str, ...]]:
    c: dict[str, tuple[str, ...]] = {}
    for pos in ("de", "nde"):
        vib = tuple(f"vib_{pos}_{k}_mms" for k in ("rms", "1x", "2x"))
        for v in vib:
            c[v] = tuple(x for x in vib if x != v) + (f"temp_bearing_{pos}_c", "noise_dba")
        c[f"temp_bearing_{pos}_c"] = (f"vib_{pos}_rms_mms", "noise_dba", "coastdown_time_s")
    c["insulation_resistance_mohm"] = ("current_imbalance_pct", "temp_winding_c")
    c["temp_winding_c"] = ("insulation_resistance_mohm", "current_imbalance_pct")
    c["current_imbalance_pct"] = ("insulation_resistance_mohm", "temp_winding_c")
    c["airflow_m3s"] = ("diff_pressure_pa", "current_l1_a", "current_l2_a", "current_l3_a", "noise_dba")
    c["diff_pressure_pa"] = ("airflow_m3s", "current_l1_a", "noise_dba")
    c["noise_dba"] = ("vib_de_rms_mms", "vib_nde_rms_mms", "airflow_m3s")
    c["coastdown_time_s"] = ("temp_bearing_de_c", "temp_bearing_nde_c", "vib_de_rms_mms", "vib_nde_rms_mms")
    return c


CORROBORATE = _corroboration()


def _notable(runs: pd.DataFrame, k: float) -> dict[str, pd.Series]:
    """Per parameter: did the value move notably vs the median of the previous 3 values (causal)?"""
    out: dict[str, pd.Series] = {}
    wanted = {q for qs in CORROBORATE.values() for q in qs}
    for p in wanted:
        flags = pd.Series(False, index=runs.index)
        for _, g in runs.groupby(["fan_id", seg_col(PARAM_GROUP.get(p, "vib"))], sort=False):
            dom = detection_domain(g.sort_values("ts"), p).dropna()
            vals, idx = dom.to_numpy(), dom.index.to_numpy()
            for j in range(2, len(vals)):
                m, s = _scale(vals[max(0, j - 3):j], p)
                flags[idx[j]] = abs(vals[j] - m) > k * s
        out[p] = flags
    return out


def clean_runs(runs: pd.DataFrame, cfg: CleaningCfg) -> tuple[pd.DataFrame, dict[int, dict[str, str]]]:
    """Return (runs with suspect values set to NaN, {row index: {parameter: kind}})."""
    out = runs.copy()
    suspects: dict[int, dict[str, str]] = {}

    def mark(i: int, p: str, kind: str) -> None:
        suspects.setdefault(int(i), {})[p] = kind
        out.at[i, p] = np.nan

    # Same-run physics rules.
    for p in NONZERO_WHEN_RUNNING:
        for i in runs.index[runs[p] == 0]:
            mark(i, p, "stuck_zero")
    ph = runs[list(PHASES)]
    med = ph.median(axis=1)
    for p in PHASES:
        bad = (ph[p] - med).abs() > cfg.phase_tolerance * med
        for i in runs.index[bad.fillna(False)]:
            mark(i, p, "inconsistent")

    notable = _notable(out, cfg.corroborate_k)

    # One-sided jump test with confirmation, per fan segment.
    for p in MEASUREMENT_FIELDS:
        for _, g in out.groupby(["fan_id", seg_col(PARAM_GROUP.get(p, "vib"))], sort=False):
            g = g.sort_values("ts")
            dom = detection_domain(g, p)
            accepted: list[float] = []
            pending: tuple[Any, float, float] | None = None  # (row, value, scale) of an unconfirmed jump
            for i, x in dom.items():
                if pd.isna(x):
                    continue
                if pending is not None:
                    _, pv, ps = pending
                    pending = None
                    if abs(x - pv) <= cfg.spike_k * ps:
                        accepted += [pv, float(x)]  # confirmed step: both values are real
                        continue
                if len(accepted) >= 2:
                    m, s = _scale(np.array(accepted[-3:]), p)
                    if abs(x - m) > cfg.spike_k * s:
                        if any(q in notable and bool(notable[q].get(i, False)) for q in CORROBORATE.get(p, ())):
                            accepted.append(float(x))  # corroborated by a related parameter: real change
                            continue
                        mark(i, p, "unconfirmed")
                        pending = (i, float(x), s)
                        continue
                accepted.append(float(x))
    return out, suspects
