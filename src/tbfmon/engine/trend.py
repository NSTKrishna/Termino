"""Layer 6 — degradation trend and time-to-ALARM.

For each key indicator, fit a Theil–Sen line (median of pairwise slopes; robust
to the odd bad reading) to the last runs of the indicator's current segment against
calendar days, cumulative run-hours and cumulative starts.

* fewer than `min_runs` values or a span under `min_span_days` → "insufficient data"
* slope confidence interval includes 0 (or points the healthy way) → "no significant trend"
* otherwise "degrading": time-to-ALARM = (alarm − current fitted level) / slope,
  with a CI from the slope interval (upper bound may be unbounded)

Idle fans degrade mostly with calendar time (grease ageing, moisture, dust), so
the calendar estimate drives decisions; run-hours/starts are reported alongside.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy.stats import theilslopes

from ..limits import Limit
from .config import TrendCfg
from .features import FEATURES, GROUP_OF, seg_col

# indicator feature → (limits.yaml parameter, threshold field, expected basis, transform)
INDICATORS: dict[str, tuple[str, str, str, str]] = {
    "vib_de_rms": ("vib_de_rms_mms", "alarm_high", "absolute", "id"),
    "vib_nde_rms": ("vib_nde_rms_mms", "alarm_high", "absolute", "id"),
    "temp_de_rise": ("temp_bearing_de_c", "alarm_high", "rise_over_ambient", "id"),
    "temp_nde_rise": ("temp_bearing_nde_c", "alarm_high", "rise_over_ambient", "id"),
    "ir40_log": ("insulation_resistance_mohm", "alarm_low", "absolute", "log10"),
    "airflow_pct": ("airflow_m3s", "alarm_low", "pct_of_rated", "id"),
    "current_imbalance": ("current_imbalance_pct", "alarm_high", "absolute", "id"),
    "coastdown": ("coastdown_time_s", "alarm_low", "absolute", "id"),
}
AXES = {"days": "calendar days", "run_hours_cum": "run-hours", "starts_cum": "starts"}


@dataclass
class TrendResult:
    indicator: str
    axis: str
    status: str                   # insufficient data | no significant trend | degrading | at/over alarm
    n: int
    slope: float | None = None    # feature units per axis unit
    slope_ci: tuple[float, float] | None = None
    level: float | None = None    # fitted current level (feature units)
    alarm: float | None = None
    eta: float | None = None      # axis units until ALARM
    eta_ci: tuple[float, float | None] | None = None   # (fast, slow); None = unbounded

    def to_dict(self) -> dict:
        return asdict(self)


def alarm_threshold(indicator: str, limits: dict[str, Limit]) -> float | None:
    param, field_, basis, tf = INDICATORS[indicator]
    lim = limits.get(param)
    if lim is None or lim.basis != basis or getattr(lim, field_) is None:
        return None
    v = float(getattr(lim, field_))
    return math.log10(v) if tf == "log10" else v


def fit(indicator: str, x: np.ndarray, y: np.ndarray, alarm: float | None, axis: str,
        cfg: TrendCfg, min_span: float) -> TrendResult:
    n = len(y)
    if n < cfg.min_runs or (x.max() - x.min()) < min_span:
        return TrendResult(indicator, axis, "insufficient data", n)
    res = theilslopes(y, x, alpha=cfg.confidence)
    slope, icpt, lo, hi = float(res.slope), float(res.intercept), float(res.low_slope), float(res.high_slope)
    level = icpt + slope * float(x.max())
    out = TrendResult(indicator, axis, "no significant trend", n, slope, (lo, hi), level, alarm)
    if alarm is None:
        return out
    bad = FEATURES[indicator].bad
    lvl_b, alarm_b = bad * level, bad * alarm
    lo_b, hi_b = (lo, hi) if bad > 0 else (-hi, -lo)
    if lvl_b >= alarm_b:
        out.status, out.eta, out.eta_ci = "at/over alarm", 0.0, (0.0, 0.0)
    elif lo_b > 0:
        gap = alarm_b - lvl_b
        out.status, out.eta = "degrading", gap / (bad * slope)
        out.eta_ci = (gap / hi_b, gap / lo_b)
    return out


def trends_for(window: pd.DataFrame, limits: dict[str, Limit], cfg: TrendCfg,
               axes: tuple[str, ...] = ("days",)) -> list[TrendResult]:
    """`window` = one fan's runs up to the assessed run (features + meta)."""
    return trend_series(window, limits, cfg, axes, only_last=True)[-1]


def trend_series(fan_rows: pd.DataFrame, limits: dict[str, Limit], cfg: TrendCfg,
                 axes: tuple[str, ...] = ("days",), only_last: bool = False) -> list[list[TrendResult]]:
    """Causal trend results for every run of one fan (time-ordered). Each indicator only
    uses runs of its own feature-group segment up to the assessed run."""
    days = ((fan_rows["ts"] - fan_rows["ts"].min()).dt.total_seconds() / 86400.0).to_numpy()
    xs = {"days": days, "run_hours_cum": fan_rows["run_hours_cum"].to_numpy(float),
          "starts_cum": fan_rows["starts_cum"].to_numpy(float)}
    ys = {ind: fan_rows[ind].to_numpy(float) for ind in INDICATORS}
    segs = {ind: fan_rows[seg_col(GROUP_OF[ind])].to_numpy() for ind in INDICATORS}
    alarms = {ind: alarm_threshold(ind, limits) for ind in INDICATORS}
    n = len(fan_rows)
    out: list[list[TrendResult]] = []
    for pos in (range(n - 1, n) if only_last else range(n)):
        res = []
        for ind in INDICATORS:
            y_all = ys[ind][: pos + 1]
            ok = np.where(~np.isnan(y_all) & (segs[ind][: pos + 1] == segs[ind][pos]))[0][-cfg.max_runs:]
            for axis in axes:
                if len(ok) == 0:
                    res.append(TrendResult(ind, axis, "insufficient data", 0))
                    continue
                min_span = float(cfg.min_span_days) if axis == "days" else 1e-9
                res.append(fit(ind, xs[axis][ok], y_all[ok], alarms[ind], axis, cfg, min_span))
        out.append(res)
    return out


def soonest(results: list[TrendResult]) -> TrendResult | None:
    """Calendar-axis indicator closest to ALARM (degrading or already over)."""
    cand = [r for r in results if r.axis == "days" and r.status in ("degrading", "at/over alarm")]
    return min(cand, key=lambda r: r.eta) if cand else None
