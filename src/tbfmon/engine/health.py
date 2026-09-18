"""Layer 7 — Health Index (0–100) and status.

HI = 100 − Σ weight_c · component_c, each component ∈ [0, 1] (weights in engine.yaml,
summing to 100; defaults below):

| component | weight | definition |
|---|---|---|
| limits | 30 | worst limit status of the run: OK 0 · WARN 0.5 · ALARM 1 (suspect values excluded) |
| drift | 20 | max over key features of bad-direction z / 6, +0.25 if EWMA or CUSUM flagged |
| anomaly | 15 | (Isolation-Forest percentile − 0.5) / 0.5 |
| fault_mode | 15 | top combined fault-mode confidence |
| trend | 15 | calendar time-to-ALARM < 90 d → 1, < 180 d → 0.6, < 365 d → 0.3 |
| peers | 5 | max over key features of bad-direction peer z / 6 |

Status: HI ≥ 80 Healthy · ≥ 60 Watch · ≥ 40 Degraded · else Critical; any limit
ALARM ⇒ Critical, any WARN ⇒ at best Watch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .config import HealthIndexCfg
from .features import FEATURE_NAMES, FEATURES

KEY = [n for n in FEATURE_NAMES if FEATURES[n].key]
STATUSES = ("Healthy", "Watch", "Degraded", "Critical")


@dataclass
class HealthResult:
    index: float
    status: str
    components: dict[str, float]
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def trend_component(eta_days: float | None) -> float:
    if eta_days is None or np.isnan(eta_days):
        return 0.0
    return 1.0 if eta_days < 90 else 0.6 if eta_days < 180 else 0.3 if eta_days < 365 else 0.0


def health(row: pd.Series, limit_worst: str, anomaly_pct: float, mode: tuple[str, float] | None,
           trend: tuple[str, float] | None, cfg: HealthIndexCfg) -> HealthResult:
    reasons: list[str] = []
    comp: dict[str, float] = {}

    comp["limits"] = {"WARN": 0.5, "ALARM": 1.0}.get(limit_worst, 0.0)
    if comp["limits"]:
        reasons.append(f"LIMIT_{limit_worst}")

    zb = {k: row.get(f"zb_{k}", np.nan) for k in KEY}
    zb = {k: v for k, v in zb.items() if pd.notna(v)}
    worst_k = max(zb, key=zb.get) if zb else None
    drift_flags = [k for k in KEY if bool(row.get(f"drift_{k}", False))]
    d = (max(0.0, zb[worst_k]) / 6.0 if worst_k else 0.0) + (0.25 if drift_flags else 0.0)
    comp["drift"] = float(min(1.0, d))
    if worst_k and zb[worst_k] >= 3:
        reasons.append(f"DEVIATION:{worst_k}(z={row.get(f'z_{worst_k}', zb[worst_k]):+.1f})")
    if drift_flags:
        reasons.append("DRIFT:" + ",".join(drift_flags[:3]))

    comp["anomaly"] = float(np.clip((anomaly_pct - 0.5) / 0.5, 0, 1)) if pd.notna(anomaly_pct) else 0.0
    if pd.notna(anomaly_pct) and anomaly_pct >= 0.98:
        reasons.append(f"ANOMALY(p={anomaly_pct:.2f})")

    comp["fault_mode"] = float(mode[1]) if mode else 0.0
    if mode and mode[1] >= 0.5:
        reasons.append(f"MODE:{mode[0]}({mode[1]:.2f})")

    comp["trend"] = trend_component(trend[1] if trend else None)
    if trend and comp["trend"] > 0:
        reasons.append(f"TREND:{trend[0]}→ALARM~{trend[1]:.0f}d")

    pzb = [row.get(f"pzb_{k}", np.nan) for k in KEY]
    pzb = [v for v in pzb if pd.notna(v)]
    comp["peers"] = float(min(1.0, max(0.0, max(pzb)) / 6.0)) if pzb else 0.0

    hi = 100.0 - sum(cfg.weights[k] * v for k, v in comp.items())
    hi = round(float(np.clip(hi, 0, 100)), 1)
    s = cfg.status
    status = "Healthy" if hi >= s.healthy else "Watch" if hi >= s.watch else "Degraded" if hi >= s.degraded else "Critical"
    if cfg.alarm_is_critical and limit_worst == "ALARM":
        status = "Critical"
    elif cfg.warn_is_watch and limit_worst == "WARN" and status == "Healthy":
        status = "Watch"
    if not bool(row.get("baseline_established", False)):
        reasons.append("LIMITED_DATA:baseline_not_established")
    return HealthResult(hi, status, {k: round(v, 3) for k, v in comp.items()}, reasons)
