"""Layer 4 — Isolation Forest per capacity class on baseline-relative z-scores.

* Input: signed robust z of every feature (missing ⇒ 0, i.e. "at baseline").
* Trained only on healthy runs (labels.healthy_mask) with an established baseline.
* Score = percentile of the run's anomaly score among the healthy training runs
  (0.5 ≈ typical, 0.99 = more unusual than 99 % of healthy runs).
* Contributions by occlusion: reset one feature to baseline (z = 0) and measure
  how much the anomaly score drops. Faithful to the model, easy to read.
* mode "oof": GroupKFold by fan — a fan is always scored by a model that never
  saw it (used by `tbfmon evaluate`); mode "full": one model per class.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from .config import AnomalyCfg
from .features import FEATURE_NAMES
from .folds import fan_folds

MIN_TRAIN = 30


@dataclass
class AnomalyResult:
    percentile: pd.Series                               # NaN where not scored
    contributors: dict[int, list[tuple[str, float, float]]]  # run → [(feature, contribution, z)]
    info: dict = field(default_factory=dict)


def _matrix(z: pd.DataFrame) -> np.ndarray:
    return z[[f"z_{n}" for n in FEATURE_NAMES]].fillna(0.0).to_numpy()


def _fit(x: np.ndarray, cfg: AnomalyCfg, seed: int) -> IsolationForest:
    return IsolationForest(n_estimators=cfg.n_estimators, random_state=seed, contamination="auto").fit(x)


def _score(model: IsolationForest, x: np.ndarray, train_scores: np.ndarray, top_k: int):
    s = -model.score_samples(x)
    pct = np.searchsorted(np.sort(train_scores), s, side="left") / len(train_scores)
    n, d = x.shape
    occl = np.repeat(x, d, axis=0)
    occl[np.arange(n * d), np.tile(np.arange(d), n)] = 0.0
    drop = s.repeat(d) - (-model.score_samples(occl))
    drop = drop.reshape(n, d)
    contrib = []
    for i in range(n):
        order = np.argsort(-drop[i])[:top_k]
        contrib.append([(FEATURE_NAMES[j], float(drop[i, j]), float(x[i, j])) for j in order if drop[i, j] > 0])
    return pct, contrib


def detect(feats: pd.DataFrame, z: pd.DataFrame, fans: pd.DataFrame, healthy: pd.Series,
           cfg: AnomalyCfg, seed: int, mode: str = "full", n_splits: int = 5) -> AnomalyResult:
    x_all = _matrix(z)
    scored = z["baseline_established"].to_numpy()
    train_ok = scored & healthy.to_numpy()
    pct = pd.Series(np.nan, index=feats.index)
    contributors: dict[int, list] = {}
    info: dict = {"classes": {}}
    cls = fans["capacity_class"].reindex(feats["fan_id"]).to_numpy()
    groups = feats["fan_id"].to_numpy()

    for c in np.unique(cls):
        in_c = cls == c
        pool = "class"
        if (train_ok & in_c).sum() < MIN_TRAIN:  # too few healthy runs in class → pool all classes
            in_train = np.ones_like(in_c)
            pool = "fleet (class too small)"
        else:
            in_train = in_c
        targets = np.where(in_c & scored)[0]
        if len(targets) == 0:
            continue
        if mode == "oof":
            for _, test_pos in fan_folds(groups[targets], n_splits):
                test = targets[test_pos]
                held = np.isin(groups, np.unique(groups[test]))
                tr = np.where(in_train & train_ok & ~held)[0]
                if len(tr) < MIN_TRAIN:
                    continue
                model = _fit(x_all[tr], cfg, seed)
                p, cb = _score(model, x_all[test], -model.score_samples(x_all[tr]), cfg.top_k)
                pct.iloc[test] = p
                for i, cc in zip(test, cb):
                    contributors[int(feats.index[i])] = cc
        else:
            tr = np.where(in_train & train_ok)[0]
            if len(tr) < MIN_TRAIN:
                continue
            model = _fit(x_all[tr], cfg, seed)
            p, cb = _score(model, x_all[targets], -model.score_samples(x_all[tr]), cfg.top_k)
            pct.iloc[targets] = p
            for i, cc in zip(targets, cb):
                contributors[int(feats.index[i])] = cc
        info["classes"][str(c)] = {"training_pool": pool, "healthy_training_runs": int((in_train & train_ok).sum())}
    return AnomalyResult(pct, contributors, info)
