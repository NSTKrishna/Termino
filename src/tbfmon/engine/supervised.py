"""Layer 5b — light supervised fault-mode model (RandomForest).

* Labels come from FailureRecords only (labels.failure_labels): runs 1–180 days
  before a failure carry its mode, runs with no failure within a year are
  "healthy", the rest are excluded. Sudden "other" failures have no precursors
  and are not a class.
* Inputs: baseline-relative z of every feature, peer z of key features and the
  run-to-run scatter. RandomForest handles missing values natively (sklearn ≥ 1.4)
  and `class_weight="balanced"` compensates for how rare failures are.
* Validation splits by fan (GroupKFold) — never by random run, because runs of
  one fan are strongly correlated and a random split would leak.
* This path is secondary to the rules: with a handful of failures per mode it
  cannot be trusted alone; `info["warnings"]` says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier

from .config import SupervisedCfg
from .features import FEATURE_NAMES, FEATURES
from .folds import fan_folds
from .rules import MODES

MIN_FAILURES_PER_MODE = 3


@dataclass
class SupervisedResult:
    proba: pd.DataFrame                  # columns = MODES + ["healthy"]; NaN where not predicted
    labels: pd.Series                    # training labels (None = excluded)
    info: dict = field(default_factory=dict)


def feature_columns() -> list[str]:
    return ([f"z_{n}" for n in FEATURE_NAMES] + [f"pz_{n}" for n in FEATURE_NAMES if FEATURES[n].key]
            + ["vib_de_cv3"])


def _model(cfg: SupervisedCfg, seed: int) -> RandomForestClassifier:
    return RandomForestClassifier(n_estimators=cfg.n_estimators, max_depth=cfg.max_depth,
                                  min_samples_leaf=cfg.min_samples_leaf, class_weight="balanced",
                                  random_state=seed, n_jobs=1)


def _place(proba: pd.DataFrame, idx: np.ndarray, model: RandomForestClassifier, x: np.ndarray) -> None:
    p = model.predict_proba(x)
    block = np.zeros((len(idx), proba.shape[1]))
    for j, c in enumerate(model.classes_):
        block[:, proba.columns.get_loc(c)] = p[:, j]
    proba.iloc[idx] = block


def train_predict(table: pd.DataFrame, labels: pd.Series, failures: pd.DataFrame, cfg: SupervisedCfg,
                  seed: int, mode: str = "full") -> SupervisedResult:
    cols = feature_columns()
    x = table[cols].to_numpy(dtype=float)
    y = labels.to_numpy(dtype=object)
    labelled = pd.notna(labels).to_numpy()
    groups = table["fan_id"].to_numpy()
    proba = pd.DataFrame(np.nan, index=table.index, columns=MODES + ["healthy"])

    fails = failures[failures["failure_mode"] != "other"]
    per_mode = fails.groupby("failure_mode")["fan_id"].agg(["count", "nunique"])
    info: dict = {
        "failures_per_mode": {m: int(per_mode["count"].get(m, 0)) for m in MODES},
        "labelled_runs": {str(k): int(v) for k, v in labels.value_counts(dropna=True).items()},
        "warnings": [f"{m}: only {int(per_mode['count'].get(m, 0))} recorded failure(s) — model unreliable"
                     for m in MODES if per_mode["count"].get(m, 0) < MIN_FAILURES_PER_MODE],
        "validation": "GroupKFold by fan" if mode == "oof" else "trained on all fans",
    }
    if len(set(y[labelled])) < 2:
        info["warnings"].append("fewer than two classes labelled — supervised path disabled")
        return SupervisedResult(proba, labels, info)

    if mode == "oof":
        oof_pred = pd.Series(None, index=table.index, dtype=object)
        for tr_all, te in fan_folds(groups, cfg.n_splits):
            tr = tr_all[labelled[tr_all]]
            if len(set(y[tr])) < 2:
                continue
            m = _model(cfg, seed).fit(x[tr], y[tr])
            _place(proba, te, m, x[te])
        lab = labelled & proba["healthy"].notna().to_numpy()
        oof_pred[lab] = proba[lab].idxmax(axis=1)
        info["oof_run_metrics"] = _per_class(pd.Series(y[lab]), oof_pred[lab].reset_index(drop=True))
    else:
        m = _model(cfg, seed).fit(x[labelled], y[labelled])
        _place(proba, np.arange(len(table)), m, x)
        imp = sorted(zip(cols, m.feature_importances_), key=lambda t: -t[1])[:8]
        info["top_features"] = [(c, round(float(v), 3)) for c, v in imp]
    return SupervisedResult(proba, labels, info)


def _per_class(y: pd.Series, pred: pd.Series) -> dict[str, dict[str, float | int]]:
    out = {}
    for c in sorted(set(y) | set(pred)):
        tp = int(((y == c) & (pred == c)).sum())
        n_pred, n_true = int((pred == c).sum()), int((y == c).sum())
        out[str(c)] = {"support": n_true, "precision": round(tp / n_pred, 3) if n_pred else None,
                       "recall": round(tp / n_true, 3) if n_true else None}
    return out
