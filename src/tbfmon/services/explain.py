"""Why a fan was flagged: contributing evidence and similar past failures.

Similar failures use case-based matching on *harmful* deviations: for the fan now,
and for each recorded failure (the last runs in the 180 days before it), take the
bad-direction z of every key feature (clipped at 0, mean of the last 3 runs) and
compare the two vectors by cosine similarity. Only records from the database are
used — never simulator ground truth.
"""

from __future__ import annotations

import datetime as dt
import warnings
from typing import Any

import numpy as np
import pandas as pd

from ..engine.features import FEATURE_NAMES, FEATURES
from ..engine.pipeline import explain_run
from .analysis import recommendation_for
from .errors import STATUS_EXIT, TbfmonError
from .workspace import Workspace

KEY = [f for f in FEATURE_NAMES if FEATURES[f].key]
LOOKBACK_DAYS = 180
MIN_SIMILARITY = 0.5


def _signature(rows: pd.DataFrame) -> np.ndarray:
    z = rows[[f"zb_{k}" for k in KEY]].tail(3).clip(lower=0)
    if not len(z):
        return np.zeros(len(KEY))
    with warnings.catch_warnings():  # features never measured in these runs → NaN → 0
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nan_to_num(np.nanmean(z.to_numpy(dtype=float), axis=0), nan=0.0)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(a @ b / (na * nb)) if na > 0 and nb > 0 else 0.0


def similar_failures(ws: Workspace, fan_id: str, as_of: str | None = None, k: int = 3) -> tuple[list[dict], np.ndarray]:
    a = ws.assessment(as_of)
    t = a.table
    now = _signature(t[t["fan_id"] == fan_id])
    out = []
    if not now.any():
        return out, now
    fails = a.data.failures[a.data.failures["failure_mode"] != "other"]
    for f in fails.itertuples():
        runs = t[(t["fan_id"] == f.fan_id) & (t["ts"] < f.date)
                 & (t["ts"] >= f.date - pd.Timedelta(days=LOOKBACK_DAYS))]
        sig = _signature(runs)
        sim = _cosine(now, sig)
        if sim < MIN_SIMILARITY:
            continue
        shared = sorted(((min(now[j], sig[j]), KEY[j]) for j in range(len(KEY)) if now[j] > 1 and sig[j] > 1),
                        reverse=True)[:3]
        out.append({
            "fan_id": f.fan_id, "date": f.date.date().isoformat(), "failure_mode": f.failure_mode,
            "similarity": round(sim, 2), "shared_deviations": [FEATURES[n].label for _, n in shared],
            "symptoms": [f"{s['parameter']} {s['direction']}" for s in f.symptoms][:4],
            "root_cause": f.root_cause, "fix": f.fix, "same_fan": f.fan_id == fan_id,
        })
    out.sort(key=lambda r: -r["similarity"])
    return out[:k], now


def explain_fan(ws: Workspace, fan: str, as_of: str | None = None) -> dict[str, Any]:
    fan_id = ws.fan_id(fan)
    a = ws.assessment(as_of)
    i = a.latest_index(fan_id)
    if i is None:
        raise TbfmonError(f"{fan_id} has no test runs to explain")
    e = explain_run(a, i)
    row = a.table.loc[i]
    contrib: dict[str, dict] = {}

    def add(feature: str, source: str) -> None:
        d = next((x for x in e["drift"] if x["feature"] == feature), None)
        c = contrib.setdefault(feature, {"feature": feature, "label": FEATURES[feature].label,
                                         "unit": FEATURES[feature].unit, "value": d and d["value"],
                                         "baseline": d and d["baseline"], "z": d and d["z"],
                                         "peer_z": d and d["peer_z"], "drift_flag": bool(d and d["flag"]),
                                         "sources": []})
        if source not in c["sources"]:
            c["sources"].append(source)

    for d in e["drift"]:
        if (d["z"] is not None and abs(d["z"]) >= a.cfg.drift.z_watch) or d["flag"]:
            add(d["feature"], "drift (own baseline)" if not d["flag"] else "drift (EWMA/CUSUM)")
        if d["peer_z"] is not None and abs(d["peer_z"]) >= 4:
            add(d["feature"], "peers")
    for c in e["anomaly"]["top_contributors"]:
        if e["anomaly"]["flagged"]:
            add(c["feature"], "anomaly model")
    features = sorted(contrib.values(), key=lambda c: -max(abs(c["z"] or 0), abs(c["peer_z"] or 0) * 0.7))
    similar, _ = similar_failures(ws, fan_id, as_of)
    rec = recommendation_for(a, i, ws)
    return {
        "fan_id": fan_id, "run_ts": row["ts"].isoformat(), "as_of": a.data.as_of.isoformat(),
        "health": e["health"], "recommendation": rec.to_dict(),
        "limit_exceedances": [x for x in e["limits"] if x["status"] in ("WARN", "ALARM")],
        "suspect_values": e["suspect_values"],
        "contributing_features": features,
        "fault_modes": e["fault_modes"],
        "anomaly": e["anomaly"],
        "similar_past_failures": similar,
        "trends": [tr for tr in e["trends"] if tr["status"] in ("degrading", "at/over alarm")],
        "exit_code": STATUS_EXIT[e["health"]["status"]],
        "generated_at": dt.datetime.now(ws.settings.tz).replace(microsecond=0).isoformat(),
    }
