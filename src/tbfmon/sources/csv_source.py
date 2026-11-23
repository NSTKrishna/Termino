"""CSV files: a bundle directory or a single file. Column contract: DESIGN.md §3."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterator

import pandas as pd

from .base import KIND_ORDER, GroundTruthRefused, Kind, RawRecord

FILE_KINDS: dict[str, Kind] = {
    "fans.csv": "fan",
    "pm_schedule.csv": "pm_task",
    "test_runs.csv": "test_run",
    "maintenance.csv": "maintenance",
    "failures.csv": "failure",
}
KIND_FILES: dict[Kind, str] = {v: k for k, v in FILE_KINDS.items()}


class CsvSource:
    def __init__(self, path: str | Path, kind: Kind | None = None):
        self.path = Path(path).expanduser().resolve()
        # Ground truth is kept separate (DESIGN.md §0.3): refuse it outright.
        if any("ground_truth" in part.lower() for part in self.path.parts):
            raise GroundTruthRefused(f"refusing to ingest ground truth: {self.path}")
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        if self.path.is_file():
            kind = kind or FILE_KINDS.get(self.path.name)
            if kind is None:
                raise ValueError(f"cannot infer record kind from {self.path.name!r}; pass --kind "
                                 f"({', '.join(KIND_ORDER)})")
            self.files: list[tuple[Kind, Path]] = [(kind, self.path)]
        else:
            self.files = [(k, self.path / KIND_FILES[k]) for k in KIND_ORDER if (self.path / KIND_FILES[k]).exists()]
            if not self.files:
                raise ValueError(f"no known CSV files in {self.path} (expected any of {', '.join(FILE_KINDS)})")
        self.name = f"csv:{self.path}"

    def records(self) -> Iterator[RawRecord]:
        for kind, file in self.files:
            df = pd.read_csv(file, dtype=str, keep_default_na=False, na_values=[""])
            for i, row in enumerate(df.to_dict("records")):
                data = {k.strip(): _clean(v) for k, v in row.items()}
                yield RawRecord(kind, data, f"{file.name}:{i + 2}")


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    v = str(v).strip()
    return v or None
