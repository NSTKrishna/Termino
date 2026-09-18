"""DESIGN.md §0.3: only `tbfmon.evaluation` may read ground truth; the engine never touches it.

Static checks: which modules may mention it, and that the engine never imports the
evaluator. Dynamic check: run the whole engine with every file-open that names
ground truth rigged to fail.
"""

from __future__ import annotations

import builtins
import io
import re
from pathlib import Path

import pandas as pd
import pytest

import tbfmon
from tbfmon import db
from tbfmon.ingest import ingest
from tbfmon.simulate import generate
from tbfmon.sources import SimulatorSource

SRC = Path(tbfmon.__file__).parent
ALLOWED = {
    "simulate/generator.py",        # writes it
    "simulate/__init__.py",         # re-exports the file name
    "paths.py",                     # workspace layout
    "sources/csv_source.py",        # refuses it
    "sources/simulator_source.py",  # documents that it never yields it
    "cli.py",                       # prints where it was written
    "cli_engine.py",                # passes the directory to the evaluator
    "evaluation/evaluate.py",       # the ONLY reader
}


def _mentions(p: Path) -> bool:
    return "ground_truth" in p.read_text(encoding="utf-8").lower()


def test_only_allowed_modules_reference_ground_truth():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if _mentions(p) and str(p.relative_to(SRC)) not in ALLOWED]
    assert offenders == []


def test_engine_never_mentions_ground_truth_or_imports_evaluator():
    for p in (SRC / "engine").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "ground_truth" not in text.lower(), p
        assert not re.search(r"(from|import)\s+[.\w]*evaluation", text), p


@pytest.fixture
def home_with_ground_truth(tmp_path):
    ds = generate(n_fans=12, years=2, seed=21)
    ds.write(tmp_path / "sim", tmp_path / "ground_truth")
    conn = db.connect(tmp_path / "t.db", create=True)
    db.init_schema(conn)
    ingest(conn, SimulatorSource(ds))
    return tmp_path, conn


def test_engine_runs_without_touching_ground_truth(home_with_ground_truth, monkeypatch):
    home, conn = home_with_ground_truth
    touched: list[str] = []

    def guard(fn):
        def wrapped(path, *a, **k):
            if "ground_truth" in str(path):
                touched.append(str(path))
                raise AssertionError(f"engine touched ground truth: {path}")
            return fn(path, *a, **k)
        return wrapped

    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr(pd, "read_csv", guard(pd.read_csv))
    original_read_text = Path.read_text

    def read_text(self, *a, **k):
        if "ground_truth" in str(self):
            touched.append(str(self))
            raise AssertionError(f"engine touched ground truth: {self}")
        return original_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)

    from tbfmon.engine import assess, load_engine_config, load_fleet, recommend_all
    from tbfmon.limits import load_limits
    from tbfmon.pm import data_as_of

    data = load_fleet(conn, load_limits(), data_as_of(conn))
    cfg = load_engine_config()
    for mode in ("full", "oof"):
        a = assess(data, cfg, mode)
        recommend_all(a)
    assert touched == []
