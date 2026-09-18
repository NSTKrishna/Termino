"""Layer 0 — causal suspect-value screening.

Same rules as `tbfmon validate` (DESIGN.md §7.9) but strictly causal: a run is
judged using only earlier runs, so point-in-time evaluation is honest.

* stuck_zero   — exact zero where a running fan cannot read zero
* inconsistent — one phase current >30 % from the median of the three
* unconfirmed  — jumps vs the previous (accepted) values of the same segment;
                 if the *next* value agrees with it, the jump is accepted as real
                 for all later windows (it stays suspect for its own run).
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
                        mark(i, p, "unconfirmed")
                        pending = (i, float(x), s)
                        continue
                accepted.append(float(x))
    return out, suspects
