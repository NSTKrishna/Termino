"""Run labels derived ONLY from FailureRecord / MaintenanceRecord (never ground truth)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import LabelsCfg, SupervisedCfg


def _days_until_next(feats: pd.DataFrame, events: pd.DataFrame) -> pd.Series:
    """Days from each run to the next event (same fan, event date ≥ run date); NaN if none."""
    out = pd.Series(np.nan, index=feats.index)
    run_day = feats["ts"].dt.normalize()
    for fan_id, ev in events.groupby("fan_id"):
        mask = feats["fan_id"] == fan_id
        d = np.sort(ev["date"].dt.normalize().to_numpy())
        rd = run_day[mask].to_numpy()
        j = np.searchsorted(d, rd, side="left")
        nxt = np.where(j < len(d), d[np.minimum(j, len(d) - 1)], np.datetime64("NaT"))
        out[mask] = (pd.to_datetime(nxt) - pd.to_datetime(rd)).days
    return out


def abnormal_pm(maintenance: pd.DataFrame, cfg: LabelsCfg) -> pd.DataFrame:
    pm = maintenance[maintenance["type"] == "preventive"]
    text = pm["findings"].fillna("").str.lower()
    hit = np.zeros(len(pm), dtype=bool)
    for kw in cfg.abnormal_findings:
        hit |= text.str.contains(kw, regex=False).to_numpy()
    return pm[hit]


def healthy_mask(feats: pd.DataFrame, failures: pd.DataFrame, maintenance: pd.DataFrame,
                 cfg: LabelsCfg) -> pd.Series:
    """Runs not shortly before a failure, a corrective repair or a PM with abnormal findings."""
    ok = pd.Series(True, index=feats.index)
    for events, days in (
        (failures, cfg.exclude_days_before_failure),
        (maintenance[maintenance["type"] == "corrective"], cfg.exclude_days_before_corrective),
        (abnormal_pm(maintenance, cfg), cfg.exclude_days_before_abnormal_pm),
    ):
        if len(events):
            dd = _days_until_next(feats, events)
            ok &= ~(dd <= days)
    return ok


def failure_labels(feats: pd.DataFrame, failures: pd.DataFrame, cfg: SupervisedCfg) -> pd.Series:
    """Per run: failure mode if 1..lookback days before a recorded failure; 'healthy' if no
    failure within `ambiguous_days`; None (excluded) otherwise. Sudden 'other' failures have no
    precursors and are not used as a class."""
    labels = pd.Series("healthy", index=feats.index, dtype=object)
    fails = failures[failures["failure_mode"] != "other"]
    for _, f in failures.iterrows():  # any failure makes nearby runs non-healthy
        mask = feats["fan_id"] == f["fan_id"]
        days = (f["date"].normalize() - feats.loc[mask, "ts"].dt.normalize()).dt.days
        labels[days[(days >= 0) & (days <= cfg.ambiguous_days)].index] = None
    for _, f in fails.iterrows():
        mask = feats["fan_id"] == f["fan_id"]
        days = (f["date"].normalize() - feats.loc[mask, "ts"].dt.normalize()).dt.days
        labels[days[(days >= 1) & (days <= cfg.lookback_days)].index] = f["failure_mode"]
    return labels
