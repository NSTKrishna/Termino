"""DESIGN.md §0.3: the simulator's ground truth is written to a separate folder and never read.

Static check: which modules may mention it. Dynamic check: run ingestion, validation and
the fleet/fan views with every file-open that names ground truth rigged to fail.
"""

from __future__ import annotations

import builtins
import io
from pathlib import Path

import pandas as pd
import pytest

import tbfmon
from tbfmon import db
from tbfmon.ingest import ingest
from tbfmon.simulate import generate
from tbfmon.sources import CsvSource, GroundTruthRefused

SRC = Path(tbfmon.__file__).parent
ALLOWED = {
    "simulate/generator.py",        # writes it
    "simulate/__init__.py",         # re-exports the file name
    "paths.py",                     # workspace layout
    "sources/csv_source.py",        # refuses it
    "services/setup.py",            # tells the simulator where to write it
    "sources/simulator_source.py",  # documents that it never yields it
    "cli.py",                       # prints where it was written
}


def _mentions(p: Path) -> bool:
    return "ground_truth" in p.read_text(encoding="utf-8").lower()


def test_only_allowed_modules_reference_ground_truth():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if _mentions(p) and str(p.relative_to(SRC)) not in ALLOWED]
    assert offenders == []


@pytest.fixture
def home_with_ground_truth(tmp_path):
    ds = generate(n_fans=12, years=2, seed=21)
    ds.write(tmp_path / "sim", tmp_path / "ground_truth")
    conn = db.connect(tmp_path / "t.db", create=True)
    db.init_schema(conn)
    return tmp_path, conn


def test_csv_source_refuses_ground_truth(home_with_ground_truth):
    home, _ = home_with_ground_truth
    with pytest.raises(GroundTruthRefused):
        CsvSource(home / "ground_truth" / "ground_truth.csv", kind="failure")


def test_pipeline_runs_without_touching_ground_truth(home_with_ground_truth, monkeypatch):
    home, conn = home_with_ground_truth
    touched: list[str] = []

    def guard(fn):
        def wrapped(path, *a, **k):
            if "ground_truth" in str(path):
                touched.append(str(path))
                raise AssertionError(f"pipeline touched ground truth: {path}")
            return fn(path, *a, **k)
        return wrapped

    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(pd, "read_csv", guard(pd.read_csv))
    original_read_text = Path.read_text

    def read_text(self, *a, **k):
        if "ground_truth" in str(self):
            touched.append(str(self))
            raise AssertionError(f"pipeline touched ground truth: {self}")
        return original_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)

    from tbfmon.limits import load_limits
    from tbfmon.pm import data_as_of
    from tbfmon.validate import run_checks
    from tbfmon.views import fan_detail, fleet_rows

    ingest(conn, CsvSource(home / "sim"))
    as_of = data_as_of(conn)
    run_checks(conn, None, as_of)
    limits = load_limits()
    rows = fleet_rows(conn, limits, as_of)
    fan_detail(conn, rows[0]["fan_id"], limits, as_of)
    assert touched == []
