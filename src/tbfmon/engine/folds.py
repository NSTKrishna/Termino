"""Fan-grouped cross-validation folds (never split one fan's runs across train and test)."""

from __future__ import annotations

from typing import Iterator

import numpy as np
from sklearn.model_selection import GroupKFold


def fan_folds(groups: np.ndarray, n_splits: int) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield (train_positions, test_positions) with disjoint fan sets."""
    k = min(n_splits, len(np.unique(groups)))
    if k < 2:
        return
    yield from GroupKFold(n_splits=k).split(np.zeros(len(groups)), groups=groups)
