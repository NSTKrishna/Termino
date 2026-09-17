"""Workspace ("home") layout. See DESIGN.md §2."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOME = "tbfmon_data"
HOME_ENV = "TBFMON_HOME"


@dataclass(frozen=True)
class Home:
    root: Path

    @classmethod
    def resolve(cls, home: str | Path | None = None) -> "Home":
        value = home or os.environ.get(HOME_ENV) or DEFAULT_HOME
        return cls(Path(value).expanduser().resolve())

    @property
    def db_path(self) -> Path:
        return self.root / "tbfmon.db"

    @property
    def limits_path(self) -> Path:
        return self.root / "limits.yaml"

    @property
    def sim_dir(self) -> Path:
        return self.root / "sim"

    @property
    def ground_truth_dir(self) -> Path:
        # Evaluator-only. Never pass this to ingestion or analytics.
        return self.root / "ground_truth"
