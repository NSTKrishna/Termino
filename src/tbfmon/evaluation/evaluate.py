"""`tbfmon evaluate` — the ONLY code allowed to read the simulator's ground_truth.csv.

Every result is labelled SIMULATED DATA. Method (DESIGN.md §10):

* The engine is run in "oof" mode: per-fan layers are causal (a run only sees
  earlier runs) and the Isolation Forest / RandomForest that score a fan were
  trained on other fans only (GroupKFold by fan).
* An *alert* is a run whose recommended action is targeted inspection or urgent
  corrective (the "CBM" system). A more sensitive variant also alerts on Watch.
* Threshold-only baseline: alert when any raw reading is at WARN/ALARM
  (and an ALARM-only variant). OEM baseline: what the time-based PM actually
  caught in the simulated history (episodes resolved by PM).
* Detection: a failed episode counts as detected if an alert fires on a run
  strictly before the failure date and on/after onset. Lead time = failure date −
  first such alert. Sudden "other" failures have no precursor and are reported
  separately.
* False alarm: an alert event (consecutive alerting runs of a fan) that does not
  overlap any fault episode window [onset − grace, end]. Rate per fan-year.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..engine import EngineConfig, assess, load_fleet, recommend_all
from ..engine.limit_check import check_all
from ..engine.recommend import decide_action, totals
from ..engine.rules import MODES
from ..limits import LimitsFile

LABEL = "SIMULATED DATA — synthetic fleet from `tbfmon simulate`; not evidence about real DMRC fans"
GROUND_TRUTH_FILE = "ground_truth.csv"


def read_ground_truth(ground_truth_dir: Path) -> pd.DataFrame:
    path = ground_truth_dir / GROUND_TRUTH_FILE
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — evaluation needs simulated data (`tbfmon simulate`)")
    gt = pd.read_csv(path, dtype={"location": str})
    for c in ("onset_date", "failure_date", "end_date"):
        gt[c] = pd.to_datetime(gt[c])
    return gt


def _windows(gt: pd.DataFrame, data_end: pd.Timestamp, grace_days: int) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for e in gt.itertuples():
        end = e.end_date if pd.notna(e.end_date) else data_end
        out.setdefault(e.fan_id, []).append((e.onset_date - pd.Timedelta(days=grace_days), end + pd.Timedelta(days=1)))
    return out


def score_alerts(alerts: pd.DataFrame, gt: pd.DataFrame, fan_years: float, data_end: pd.Timestamp,
                 grace_days: int) -> dict[str, Any]:
    """`alerts`: fan_id, ts, alert (bool), mode (predicted or None). One row per run, time-ordered."""
    alerts = alerts.sort_values(["fan_id", "ts"])
    res: dict[str, Any] = {}
    fails = gt[(gt["outcome"] == "failed") & (gt["fault_mode"] != "other")]
    detected, leads, per_failure = 0, [], []
    for e in fails.itertuples():
        a = alerts[(alerts["fan_id"] == e.fan_id) & alerts["alert"] & (alerts["ts"] >= e.onset_date)
                   & (alerts["ts"].dt.normalize() < e.failure_date)]
        first = a["ts"].min() if len(a) else None
        if first is not None:
            detected += 1
            leads.append((e.failure_date - first.normalize()).days)
        per_failure.append({"fault_id": e.fault_id, "first_alert": first.date().isoformat() if first is not None else None,
                            "lead_days": (e.failure_date - first.normalize()).days if first is not None else None})
    by_mode: dict[str, list[int]] = {}
    for e, pf in zip(fails.itertuples(), per_failure):
        c = by_mode.setdefault(e.fault_mode, [0, 0])
        c[0] += pf["first_alert"] is not None
        c[1] += 1
    res["failure_recall_by_mode"] = {m: f"{d}/{n}" for m, (d, n) in sorted(by_mode.items())}
    res["failures"] = int(len(fails))
    res["failures_detected"] = detected
    res["failure_recall"] = round(detected / len(fails), 3) if len(fails) else None
    res["lead_days"] = {"median": float(np.median(leads)) if leads else None,
                        "min": int(min(leads)) if leads else None, "max": int(max(leads)) if leads else None,
                        "detected_30d_ahead": int(sum(ld >= 30 for ld in leads))}

    # every precursor-bearing episode (failed, resolved by PM, or ongoing)
    eps = gt[gt["fault_mode"] != "other"]
    ep_det, mode_rows = 0, []
    for e in eps.itertuples():
        stop = e.failure_date if pd.notna(e.failure_date) else (e.end_date if pd.notna(e.end_date) else data_end)
        a = alerts[(alerts["fan_id"] == e.fan_id) & alerts["alert"] & (alerts["ts"] >= e.onset_date)
                   & (alerts["ts"].dt.normalize() < stop)]
        if len(a):
            ep_det += 1
            mode_rows.append((e.fault_mode, a.iloc[0]["mode"], a.iloc[-1]["mode"]))
    res["episodes"] = int(len(eps))
    res["episodes_detected"] = ep_det
    res["episode_recall"] = round(ep_det / len(eps), 3) if len(eps) else None

    # alert events and false alarms
    win = _windows(gt, data_end, grace_days)
    events = fa = 0
    for fan_id, g in alerts.groupby("fan_id"):
        flags = g["alert"].to_numpy()
        ts = g["ts"].to_numpy()
        k = 0
        while k < len(flags):
            if not flags[k]:
                k += 1
                continue
            j = k
            while j + 1 < len(flags) and flags[j + 1]:
                j += 1
            events += 1
            t0, t1 = pd.Timestamp(ts[k]), pd.Timestamp(ts[j])
            if not any(t0 < w1 and t1 >= w0 for w0, w1 in win.get(fan_id, [])):
                fa += 1
            k = j + 1
    res["alert_runs"] = int(alerts["alert"].sum())
    res["alert_events"] = events
    res["false_alarm_events"] = fa
    res["false_alarms_per_fan_year"] = round(fa / fan_years, 3) if fan_years else None
    res["alert_event_precision"] = round((events - fa) / events, 3) if events else None
    res["_mode_pairs"] = mode_rows
    res["_per_failure"] = per_failure
    return res


def mode_metrics(triples: list[tuple[str, str | None, str | None]], at: str = "first") -> dict[str, Any]:
    """Per-class precision/recall of the predicted mode at the first (or last) alert of each
    detected episode (last = the latest alert before failure / end of episode)."""
    pairs = [(t, f if at == "first" else last) for t, f, last in triples]
    out: dict[str, Any] = {"_at": f"{at} alert in episode"}
    for m in MODES:
        tp = sum(t == m and p == m for t, p in pairs)
        n_pred = sum(p == m for _, p in pairs)
        n_true = sum(t == m for t, _ in pairs)
        out[m] = {"support": n_true, "predicted": n_pred,
                  "precision": round(tp / n_pred, 3) if n_pred else None,
                  "recall": round(tp / n_true, 3) if n_true else None}
    named = [(t, p) for t, p in pairs if p]
    out["_overall"] = {"detected_episodes": len(pairs), "mode_named": len(named),
                       "accuracy_when_named": round(sum(t == p for t, p in named) / len(named), 3) if named else None}
    return out


def evaluate(conn, ground_truth_dir: Path, limits: LimitsFile, cfg: EngineConfig, as_of: dt.date) -> dict[str, Any]:
    gt = read_ground_truth(ground_truth_dir)
    data = load_fleet(conn, limits, as_of)
    a = assess(data, cfg, mode="oof")
    t = a.table
    min_conf = cfg.fault_modes.min_confidence
    data_start, data_end = t["ts"].min().normalize(), t["ts"].max().normalize()
    fan_years = len(data.fans) * (data_end - data_start).days / 365.25
    gt = gt[gt["fan_id"].isin(data.fans.index) & (gt["onset_date"] <= data_end)]
    grace = cfg.evaluation.pre_onset_grace_days

    # ---- CBM system alerts (per run, point-in-time)
    rows = []
    for i in t.index:
        comb = a.combined[int(i)]
        top = (comb[0][0], comb[0][1]) if comb else None
        status = a.health[int(i)].status
        action = decide_action(status, top, bool(t.at[i, "baseline_established"]), False, min_conf)
        rules_top = a.rules[int(i)][0] if a.rules[int(i)] else None
        proba = a.supervised.proba.loc[i, MODES]
        rows.append({
            "fan_id": t.at[i, "fan_id"], "ts": t.at[i, "ts"], "status": status, "action": action,
            "mode": top[0] if top and top[1] >= min_conf else None,
            "mode_rules": rules_top.mode if rules_top and rules_top.confidence >= min_conf else None,
            "mode_model": proba.idxmax() if proba.notna().any() and proba.max() >= min_conf else None,
        })
    cbm = pd.DataFrame(rows)
    for c in ("mode", "mode_rules", "mode_model"):  # keep None (NaN would count as a named mode)
        cbm[c] = cbm[c].astype(object).where(cbm[c].notna(), None)

    # ---- threshold-only baseline on raw readings (no suspect screening)
    raw_worst, _ = check_all(data.runs, data.fans, data.limits, {})
    thr = data.runs[["fan_id", "ts"]].copy()
    thr["worst"] = raw_worst.values

    systems: dict[str, pd.DataFrame] = {
        "CBM engine (inspect/urgent)": cbm.assign(alert=cbm["action"].isin(["targeted inspection", "urgent corrective"])),
        "CBM engine, sensitive (Watch+)": cbm.assign(alert=cbm["status"] != "Healthy"),
        "Threshold-only (WARN or ALARM)": thr.assign(alert=thr["worst"].isin(["WARN", "ALARM"]), mode=None),
        "Threshold-only (ALARM)": thr.assign(alert=thr["worst"] == "ALARM", mode=None),
    }
    scored = {name: score_alerts(df[["fan_id", "ts", "alert", "mode"]], gt, fan_years, data_end, grace)
              for name, df in systems.items()}

    # ---- fault-mode identification at first alert (CBM), per path
    cbm_alerts = systems["CBM engine (inspect/urgent)"]
    triples = scored["CBM engine (inspect/urgent)"]["_mode_pairs"]
    fm = {"combined_first": mode_metrics(triples, "first"), "combined_last": mode_metrics(triples, "last")}
    for path, col in (("rules", "mode_rules"), ("model", "mode_model")):
        s = score_alerts(cbm_alerts[["fan_id", "ts", "alert"]].assign(mode=cbm_alerts[col]), gt, fan_years, data_end, grace)
        fm[f"{path}_last"] = mode_metrics(s["_mode_pairs"], "last")

    # ---- OEM time-based baseline (what the simulated PM regime achieved)
    ended = gt[(gt["fault_mode"] != "other") & gt["outcome"].isin(["failed", "resolved_by_pm"])]
    pm = data.maintenance[data.maintenance["type"] == "preventive"]
    normal = ~pm["findings"].fillna("").str.lower().str.contains("|".join(cfg.labels.abnormal_findings), regex=True)
    oem = {
        "episodes_ended": int(len(ended)),
        "caught_by_pm": int((ended["outcome"] == "resolved_by_pm").sum()),
        "recall": round(float((ended["outcome"] == "resolved_by_pm").mean()), 3) if len(ended) else None,
        "pm_tasks_per_fan_year": round(len(pm) / fan_years, 2),
        "pm_tasks_with_normal_findings_pct": round(float(normal.mean()) * 100, 1) if len(pm) else None,
        "pm_man_hours_per_fan_year": round(float(pm["man_hours"].sum()) / fan_years, 1),
        "pm_height_hours_per_fan_year": round(float(pm.loc[pm["work_at_height"] == True, "man_hours"].sum()) / fan_years, 1),  # noqa: E712
        "failures_not_prevented": int((ended["outcome"] == "failed").sum()),
    }

    # ---- per-failure table (CBM vs threshold)
    fails = gt[gt["outcome"] == "failed"].sort_values("failure_date")
    cb = {r["fault_id"]: r for r in scored["CBM engine (inspect/urgent)"]["_per_failure"]}
    th = {r["fault_id"]: r for r in scored["Threshold-only (WARN or ALARM)"]["_per_failure"]}
    per_failure = []
    for e in fails.itertuples():
        pre = cbm_alerts[(cbm_alerts["fan_id"] == e.fan_id) & cbm_alerts["alert"] & (cbm_alerts["ts"] >= e.onset_date)
                         & (cbm_alerts["ts"].dt.normalize() < e.failure_date)]
        n_runs = int(((t["fan_id"] == e.fan_id) & (t["ts"] >= e.onset_date) & (t["ts"].dt.normalize() < e.failure_date)).sum())
        per_failure.append({
            "fan_id": e.fan_id, "mode": e.fault_mode, "onset": e.onset_date.date().isoformat(),
            "failure": e.failure_date.date().isoformat(), "runs_before_failure": n_runs,
            "cbm_first_alert": cb.get(e.fault_id, {}).get("first_alert"), "cbm_lead_days": cb.get(e.fault_id, {}).get("lead_days"),
            "cbm_mode_at_first_alert": pre.iloc[0]["mode"] if len(pre) else None,
            "cbm_mode_at_last_alert": pre.iloc[-1]["mode"] if len(pre) else None,
            "threshold_first_alert": th.get(e.fault_id, {}).get("first_alert"),
            "threshold_lead_days": th.get(e.fault_id, {}).get("lead_days"),
        })

    # ---- forward-looking savings from the recommender (full-mode models, as of data end)
    full = assess(data, cfg, mode="full")
    savings = totals(recommend_all(full))
    savings["per_fan_year"] = {k: round(savings[k] / len(data.fans), 1) for k in
                               ("man_hours_oem", "man_hours_cbm", "man_hours_saved", "height_hours_saved")}
    inspection_hours = scored["CBM engine (inspect/urgent)"]["alert_events"] * cfg.recommender.man_hours["targeted_inspection"]

    for s in scored.values():
        s.pop("_mode_pairs")
        s.pop("_per_failure")
    return {
        "label": LABEL,
        "as_of": as_of.isoformat(),
        "dataset": {
            "fans": int(len(data.fans)), "runs": int(len(t)), "fan_years": round(fan_years, 1),
            "episodes_by_outcome": gt["outcome"].value_counts().to_dict(),
            "episodes_by_mode": gt["fault_mode"].value_counts().to_dict(),
            "sudden_failures_no_precursor": int(((gt["fault_mode"] == "other") & (gt["outcome"] == "failed")).sum()),
        },
        "systems": scored,
        "fault_modes": fm,
        "supervised_oof_run_metrics": a.info["supervised"].get("oof_run_metrics"),
        "supervised_warnings": a.info["supervised"].get("warnings", []),
        "oem_baseline": oem,
        "cbm_inspection_hours_per_fan_year": round(inspection_hours / fan_years, 1),
        "recommender_savings_next_year": savings,
        "per_failure": per_failure,
    }
