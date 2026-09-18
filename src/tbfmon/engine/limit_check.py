"""Layer 1 — every reading vs limits.yaml → OK / WARN / ALARM with % deviation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from ..limits import Limit, LimitsFile, Status, evaluate, worst


@dataclass
class LimitResult:
    parameter: str
    value: float | None          # reading in its own unit (None if missing or suspect)
    compared: float | None       # value in the limit's basis (%, K rise, absolute)
    basis: str
    warn: float | None           # the warn threshold on the side that matters
    alarm: float | None
    side: str                    # "high" | "low"
    status: str                  # OK | WARN | ALARM | NA | SUSPECT
    pct_deviation: float | None  # (compared − warn)/|warn| · 100 toward the bad side; >0 = beyond WARN

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _side(limit: Limit, x: float | None) -> tuple[str, float | None, float | None, float | None]:
    """Pick the relevant side (for two-sided limits, the one closer to being exceeded)."""
    options = []
    if limit.warn_high is not None or limit.alarm_high is not None:
        w = limit.warn_high if limit.warn_high is not None else limit.alarm_high
        dev = None if x is None else (x - w) / abs(w) * 100.0
        options.append(("high", limit.warn_high, limit.alarm_high, dev))
    if limit.warn_low is not None or limit.alarm_low is not None:
        w = limit.warn_low if limit.warn_low is not None else limit.alarm_low
        dev = None if x is None else (w - x) / abs(w) * 100.0
        options.append(("low", limit.warn_low, limit.alarm_low, dev))
    return max(options, key=lambda o: -1e18 if o[3] is None else o[3])


def check_run(run: dict[str, Any], fan: dict[str, Any], limits: dict[str, Limit],
              suspect: dict[str, str] | None = None) -> list[LimitResult]:
    suspect = suspect or {}
    out: list[LimitResult] = []
    ambient = run.get("ambient_temp_c")
    ambient = None if ambient is None or pd.isna(ambient) else float(ambient)
    for p, lim in limits.items():
        v = run.get(p)
        v = None if v is None or pd.isna(v) else float(v)
        st, x = evaluate(p, v, lim, fan, ambient)
        side, warn, alarm, dev = _side(lim, x)
        status = "SUSPECT" if p in suspect else st.value
        out.append(LimitResult(p, v, None if x is None else round(x, 3), lim.basis, warn, alarm, side, status,
                               None if dev is None else round(dev, 1)))
    return out


def worst_status(results: list[LimitResult]) -> str:
    return worst(Status(r.status) for r in results if r.status != "SUSPECT").value


def check_all(runs_clean: pd.DataFrame, fans: pd.DataFrame, limits: LimitsFile,
              suspects: dict[int, dict[str, str]]) -> tuple[pd.Series, dict[int, list[LimitResult]]]:
    """(worst status per run, full results per run)."""
    per_class = {c: limits.for_class(c) for c in fans["capacity_class"].unique() if c in limits.classes}
    status: dict[int, str] = {}
    results: dict[int, list[LimitResult]] = {}
    fan_dicts = {fid: {"fan_id": fid, **row} for fid, row in fans.to_dict("index").items()}
    for i, row in zip(runs_clean.index, runs_clean.to_dict("records")):
        fan = fan_dicts[row["fan_id"]]
        lims = per_class.get(fan["capacity_class"], {})
        res = check_run(row, fan, lims, suspects.get(int(i)))
        results[int(i)] = res
        status[int(i)] = worst_status(res)
    return pd.Series(status).reindex(runs_clean.index), results
