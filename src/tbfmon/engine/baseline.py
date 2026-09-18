"""Layer 3 — per-fan baseline, robust z-scores, EWMA/CUSUM drift, peer comparison.

All statistics are causal (a run only sees earlier runs):
* baseline per feature = median / MAD of the first N values of the fan's current
  segment (segments restart after corrective repairs); until `min_runs` values
  exist the baseline is "not established" and z is NaN
* robust z = (x − median) / max(1.4826·MAD, noise floor); clipped to ±z_cap
* "bad-direction" z (zb) is positive when the change is in the harmful direction
* EWMA and one-sided CUSUM run on zb per segment
* peer z compares the run to the other fans of the same capacity class
  (median of each peer's values over the trailing window, month-end snapshots)
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from .config import BaselineCfg, DriftCfg, PeersCfg
from .features import FEATURE_NAMES, FEATURES, GROUP_OF, seg_col


def _scale(values: np.ndarray, name: str) -> tuple[float, float]:
    f = FEATURES[name]
    m = float(np.median(values))
    return m, max(1.4826 * float(np.median(np.abs(values - m))), f.abs_floor, f.rel_floor * abs(m))


def baseline_and_drift(feats: pd.DataFrame, bcfg: BaselineCfg, dcfg: DriftCfg) -> pd.DataFrame:
    """Columns per feature: b_<f> (baseline median), z_<f>, zb_<f>, ewma_<f>, cusum_<f>, drift_<f> (bool)."""
    n = len(feats)
    cols: dict[str, np.ndarray] = {}
    for name in FEATURE_NAMES:
        for pre in ("b", "z", "zb", "ewma", "cusum"):
            cols[f"{pre}_{name}"] = np.full(n, np.nan)
        cols[f"drift_{name}"] = np.zeros(n, dtype=bool)
    established = np.zeros(n, dtype=bool)
    pos = {idx: k for k, idx in enumerate(feats.index)}
    ewma_limit = dcfg.ewma_limit * np.sqrt(dcfg.ewma_lambda / (2 - dcfg.ewma_lambda))

    for name in FEATURE_NAMES:
        for _, g in feats.groupby(["fan_id", seg_col(GROUP_OF[name])], sort=False):
            g = g.sort_values("ts")
            rows = [pos[i] for i in g.index]
            bad = FEATURES[name].bad
            base: list[float] = []
            e, c = 0.0, 0.0
            for k, x in zip(rows, g[name].to_numpy()):
                if np.isnan(x):
                    continue
                if len(base) < bcfg.n_runs:
                    base.append(float(x))
                if len(base) < bcfg.min_runs:
                    continue
                m, s = _scale(np.array(base), name)
                z = float(np.clip((x - m) / s, -dcfg.z_cap, dcfg.z_cap))
                zb = bad * z if bad else abs(z)
                e = dcfg.ewma_lambda * zb + (1 - dcfg.ewma_lambda) * e
                c = max(0.0, c + zb - dcfg.cusum_k)
                cols[f"b_{name}"][k] = m
                cols[f"z_{name}"][k] = z
                cols[f"zb_{name}"][k] = zb
                cols[f"ewma_{name}"][k] = e
                cols[f"cusum_{name}"][k] = c
                cols[f"drift_{name}"][k] = e > ewma_limit or c > dcfg.cusum_h
    # "established" = at least one key vibration or thermal feature has a baseline
    for f in ("vib_de_rms", "vib_nde_rms", "temp_de_rise"):
        established |= ~np.isnan(cols[f"z_{f}"])
    out = pd.DataFrame(cols, index=feats.index)
    out["baseline_established"] = established
    return out


def peer_z(feats: pd.DataFrame, fans: pd.DataFrame, cfg: PeersCfg, dcfg: DriftCfg) -> pd.DataFrame:
    """pz_<f> (signed) and pzb_<f> (bad-direction) for key features, leave-one-fan-out."""
    keys = [n for n in FEATURE_NAMES if FEATURES[n].key]
    n = len(feats)
    pz = np.full((n, len(keys)), np.nan)
    cls = fans["capacity_class"].reindex(feats["fan_id"]).to_numpy()
    fan = feats["fan_id"].to_numpy()
    ts = feats["ts"].to_numpy()
    month = feats["ts"].dt.to_period("M").to_numpy()
    vals = feats[keys].to_numpy(dtype=float)
    floors = [(FEATURES[k].abs_floor, FEATURES[k].rel_floor) for k in keys]
    window = np.timedelta64(cfg.window_days, "D")
    for c in np.unique(cls):
        in_c = cls == c
        if len(np.unique(fan[in_c])) < cfg.min_peers + 1:
            continue
        for m in np.unique(month[in_c]):
            end = np.datetime64(m.start_time)  # causal: only runs before this month
            win = in_c & (ts < end) & (ts >= end - window)
            tgt = np.where(in_c & (month == m))[0]
            if not win.any() or not len(tgt):
                continue
            win_fans = np.unique(fan[win])
            with warnings.catch_warnings():  # a peer may lack a feature entirely
                warnings.simplefilter("ignore", RuntimeWarning)
                per_fan = np.vstack([np.nanmedian(vals[win & (fan == f)], axis=0) for f in win_fans])
            for f in np.unique(fan[tgt]):
                rows = tgt[fan[tgt] == f]
                peers = per_fan[win_fans != f]
                for j, (absf, relf) in enumerate(floors):
                    col = peers[:, j]
                    col = col[~np.isnan(col)]
                    if len(col) < cfg.min_peers:
                        continue
                    med = float(np.median(col))
                    sc = max(1.4826 * float(np.median(np.abs(col - med))), absf, relf * abs(med))
                    pz[rows, j] = np.clip((vals[rows, j] - med) / sc, -dcfg.z_cap, dcfg.z_cap)
    bad = np.array([FEATURES[k].bad for k in keys])
    pzb = np.where(bad == 0, np.abs(pz), pz * bad)
    return pd.DataFrame(np.hstack([pz, pzb]), index=feats.index,
                        columns=[f"pz_{k}" for k in keys] + [f"pzb_{k}" for k in keys])


def last_known(table: pd.DataFrame, max_age_days: int) -> pd.DataFrame:
    """Per run, the most recent value of every feature-derived column within the segment,
    if measured at most `max_age_days` earlier (sparse parameters like IR or airflow are
    not measured on every run). Adds `kts_<feature>` = when that value was measured."""
    cols: dict[str, pd.Series] = {}
    limit = pd.Timedelta(days=max_age_days)
    for f in FEATURE_NAMES:
        keys = [table["fan_id"], table[seg_col(GROUP_OF[f])]]
        seen = table["ts"].where(table[f].notna())
        kts = seen.groupby(keys).ffill()
        fresh = (table["ts"] - kts) <= limit
        cols[f"kts_{f}"] = kts.where(fresh)
        for pre in ("", "b_", "z_", "zb_", "pz_", "pzb_"):
            col = f"{pre}{f}"
            if col in table:
                src = table[col].where(table[f].notna())
                cols[col] = src.groupby(keys).ffill().where(fresh)
    return pd.DataFrame(cols, index=table.index)
