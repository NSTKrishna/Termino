"""Engine orchestration: runs every layer and assembles an explainable Assessment.

mode="full": models trained on all fans (what `tbfmon assess` / `recommend` show).
mode="oof" : models score each fan without having seen it (GroupKFold by fan);
             per-fan layers are causal either way. Used by `tbfmon evaluate`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .anomaly import AnomalyResult, detect
from .baseline import baseline_and_drift, last_known, peer_z
from .clean import clean_runs
from .config import EngineConfig
from .data import FleetData
from .features import compute_features
from .health import HealthResult, health
from .labels import failure_labels, healthy_mask
from .limit_check import LimitResult, check_all
from .recommend import Recommendation, recommend_fan
from .rules import MODES, ModeScore, scatter_cv, score_run
from .supervised import SupervisedResult, train_predict
from .trend import TrendResult, soonest, trend_series, trends_for


@dataclass
class Assessment:
    data: FleetData
    cfg: EngineConfig
    mode: str
    table: pd.DataFrame                          # one row per run: meta, features, z, peer z, verdicts
    suspects: dict[int, dict[str, str]]
    limit_results: dict[int, list[LimitResult]]
    anomaly: AnomalyResult
    rules: dict[int, list[ModeScore]]
    supervised: SupervisedResult
    combined: dict[int, list[tuple[str, float, str | None]]]
    trends: dict[int, list[TrendResult]]
    health: dict[int, HealthResult]
    info: dict = field(default_factory=dict)

    def latest_index(self, fan_id: str) -> int | None:
        rows = self.table[self.table["fan_id"] == fan_id]
        return int(rows.index[-1]) if len(rows) else None


def _combine(rules: list[ModeScore], proba: dict[str, float], w: float, k: int,
             unreliable: set[str] = frozenset()) -> list[tuple[str, float, str | None]]:
    """combined = w·rule + (1−w)·model. Where the model has too few recorded failures of a
    mode to be trusted, it may add evidence but never dilute the rule: max(rule, blend)."""
    rc = {m.mode: (m.confidence, m.location) for m in rules}
    out = []
    for m in MODES:
        r = rc.get(m, (0.0, None))
        p = proba.get(m)
        p = None if p is None or np.isnan(p) else float(p)
        c = r[0] if p is None else w * r[0] + (1 - w) * p
        if p is not None and m in unreliable:
            c = max(r[0], c)
        if c > 0:
            out.append((m, round(c, 3), r[1]))
    return sorted(out, key=lambda t: -t[1])[:k]


def assess(data: FleetData, cfg: EngineConfig, mode: str = "full") -> Assessment:
    runs_clean, suspects = clean_runs(data.runs, cfg.cleaning)
    feats = compute_features(runs_clean, data.fans, cfg.features)
    z = baseline_and_drift(feats, cfg.baseline, cfg.drift)
    pz = peer_z(feats, data.fans, cfg.peers, cfg.drift)
    table = pd.concat([feats, z, pz], axis=1)
    table["vib_de_cv3"] = scatter_cv(feats)

    limit_worst, limit_results = check_all(runs_clean, data.fans, data.limits, suspects)
    table["limit_worst"] = limit_worst

    healthy = healthy_mask(feats, data.failures, data.maintenance, cfg.labels)
    table["healthy_label"] = healthy
    anom = detect(feats, z, data.fans, healthy, cfg.anomaly, cfg.seed, mode, cfg.supervised.n_splits)
    table["anomaly_pct"] = anom.percentile

    labels = failure_labels(feats, data.failures, cfg.supervised)
    sup = train_predict(table, labels, data.failures, cfg.supervised, cfg.seed, mode)

    per_class = {c: data.limits.for_class(c) for c in data.fans["capacity_class"].unique() if c in data.limits.classes}
    fan_class = data.fans["capacity_class"].to_dict()

    rules: dict[int, list[ModeScore]] = {}
    combined: dict[int, list] = {}
    trends: dict[int, list[TrendResult]] = {}
    hres: dict[int, HealthResult] = {}
    unreliable = {w.split(":")[0] for w in sup.info.get("warnings", [])}
    # Rules and health read the last known state of each feature (sparse parameters).
    known = last_known(table, cfg.baseline.carry_forward_days)
    state = table.drop(columns=[c for c in known.columns if c in table.columns]).join(known)
    proba = sup.proba.to_dict("index")
    for fan_id, g in table.groupby("fan_id", sort=False):
        g = g.sort_values("ts")
        lims = per_class.get(fan_class[fan_id], {})
        seg_trends = trend_series(g, lims, cfg.trend)
        for pos, i in enumerate(g.index):
            row = state.loc[i]
            ls = {r.parameter: r.status for r in limit_results[int(i)]}
            rs = score_run(row, ls)
            rules[int(i)] = rs
            comb = _combine(rs, proba[i], cfg.fault_modes.rule_weight, cfg.fault_modes.top_k, unreliable)
            combined[int(i)] = comb
            tr = seg_trends[pos]
            trends[int(i)] = tr
            s = soonest(tr)
            h = health(row, row["limit_worst"], row["anomaly_pct"], (comb[0][0], comb[0][1]) if comb else None,
                       (s.indicator, s.eta) if s else None, cfg.health_index)
            hres[int(i)] = h
    order = [int(i) for i in table.index]
    table["top_mode"] = [combined[i][0][0] if combined[i] else None for i in order]
    table["top_conf"] = [combined[i][0][1] if combined[i] else 0.0 for i in order]
    so = {i: soonest(trends[i]) for i in order}
    table["trend_indicator"] = [so[i].indicator if so[i] else None for i in order]
    table["trend_eta_days"] = [so[i].eta if so[i] else np.nan for i in order]
    table["health_index"] = [hres[i].index for i in order]
    table["status"] = [hres[i].status for i in order]
    for c in known.columns:
        if c.startswith("kts_"):
            table[c] = known[c]
    info = {"anomaly": anom.info, "supervised": sup.info, "mode": mode, "as_of": data.as_of.isoformat(),
            "runs": len(table), "fans": int(table["fan_id"].nunique())}
    return Assessment(data, cfg, mode, table, suspects, limit_results, anom, rules, sup, combined, trends, hres, info)


def recommend_all(a: Assessment) -> list[Recommendation]:
    recs = []
    as_of = a.data.as_of
    for fan_id in a.data.fans.index:
        i = a.latest_index(fan_id)
        if i is None:
            recs.append(recommend_fan(fan_id, "Watch", None, [], None, False, None, a.data.pm_tasks,
                                      a.data.maintenance, as_of, a.cfg.recommender,
                                      a.cfg.fault_modes.min_confidence, ["NO_RUNS"]))
            continue
        row = a.table.loc[i]
        h = a.health[i]
        recs.append(recommend_fan(
            fan_id, h.status, h.index, a.combined[i],
            None if pd.isna(row["trend_eta_days"]) else float(row["trend_eta_days"]),
            bool(row["baseline_established"]), row["ts"].date(), a.data.pm_tasks, a.data.maintenance,
            as_of, a.cfg.recommender, a.cfg.fault_modes.min_confidence, h.reasons))
    return recs


def explain_run(a: Assessment, i: int) -> dict:
    """Everything the engine concluded about one run, for humans (and --json)."""
    row = a.table.loc[i]
    return {
        "fan_id": row["fan_id"], "ts": row["ts"].isoformat(), "run_type": row["run_type"],
        "baseline_established": bool(row["baseline_established"]),
        "health": a.health[i].to_dict(),
        "limits": [r.to_dict() for r in a.limit_results[i] if r.status not in ("NA",)],
        "suspect_values": a.suspects.get(i, {}),
        "drift": [
            {"feature": f, "value": _num(row.get(f)), "baseline": _num(row.get(f"b_{f}")), "z": _num(row.get(f"z_{f}")),
             "ewma": _num(row.get(f"ewma_{f}")), "cusum": _num(row.get(f"cusum_{f}")), "flag": bool(row.get(f"drift_{f}")),
             "peer_z": _num(row.get(f"pz_{f}")), "delta_prev": _num(row.get(f"d_{f}"))}
            for f in _features_with_values(row)],
        "anomaly": {"percentile": _num(row["anomaly_pct"]),
                    "flagged": bool(pd.notna(row["anomaly_pct"]) and row["anomaly_pct"] >= a.cfg.anomaly.flag_percentile),
                    "top_contributors": [{"feature": f, "contribution": round(c, 4), "z": round(zv, 2)}
                                         for f, c, zv in a.anomaly.contributors.get(i, [])]},
        "fault_modes": {
            "rules": [{"mode": m.mode, "confidence": m.confidence, "location": m.location, "reasons": m.reasons}
                      for m in a.rules[i]],
            "model": {k: round(float(v), 3) for k, v in a.supervised.proba.loc[i].dropna().sort_values(ascending=False).items()},
            "combined": [{"mode": m, "confidence": c, "location": loc} for m, c, loc in a.combined[i]],
        },
        "trends": [t.to_dict() for t in a.trends[i]],
    }


def _num(v) -> float | None:
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), 4)


def _features_with_values(row: pd.Series) -> list[str]:
    from .features import FEATURE_NAMES
    return [f for f in FEATURE_NAMES if pd.notna(row.get(f))]


def fan_trends_all_axes(a: Assessment, fan_id: str) -> list[TrendResult]:
    i = a.latest_index(fan_id)
    if i is None:
        return []
    window = a.table[(a.table["fan_id"] == fan_id) & (a.table.index <= i)].sort_values("ts")
    lims = a.data.limits.for_class(a.data.fans.loc[fan_id, "capacity_class"])
    return trends_for(window, lims, a.cfg.trend, axes=("days", "run_hours_cum", "starts_cum"))


def as_of_date(v: dt.date | str) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(v)
