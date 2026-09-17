"""Records straight from an in-memory simulator Dataset (no CSV round trip)."""

from __future__ import annotations

from typing import Iterator

import numpy as np
import pandas as pd

from ..simulate.generator import Dataset
from .base import Kind, RawRecord

_FRAMES: tuple[tuple[Kind, str], ...] = (
    ("fan", "fans"),
    ("pm_task", "pm_schedule"),
    ("test_run", "test_runs"),
    ("maintenance", "maintenance"),
    ("failure", "failures"),
)  # never ground_truth


class SimulatorSource:
    def __init__(self, dataset: Dataset):
        self.dataset = dataset
        p = dataset.params
        self.name = f"simulator:seed={p.get('seed')},fans={p.get('fans_requested')},years={p.get('years')}"

    def records(self) -> Iterator[RawRecord]:
        for kind, attr in _FRAMES:
            df: pd.DataFrame = getattr(self.dataset, attr)
            for i, row in enumerate(df.to_dict("records")):
                yield RawRecord(kind, {k: _py(v) for k, v in row.items()}, f"{attr}[{i}]")


def _py(v):
    if v is None or v is pd.NA or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, np.generic):
        v = v.item()
        if isinstance(v, float) and np.isnan(v):
            return None
    if isinstance(v, str) and not v.strip():
        return None
    return v
