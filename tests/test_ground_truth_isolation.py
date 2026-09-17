"""DESIGN.md §0.3: only the simulator (writer) and explicit guards may mention ground truth.

The analytics/ingestion code must never read ground_truth.csv. When the
evaluator is added, add its module to ALLOWED.
"""

from __future__ import annotations

from pathlib import Path

import tbfmon

SRC = Path(tbfmon.__file__).parent
ALLOWED = {
    "simulate/generator.py",      # writes it
    "simulate/__init__.py",       # re-exports the file name
    "paths.py",                   # workspace layout
    "sources/csv_source.py",      # refuses it
    "sources/simulator_source.py",  # documents that it never yields it
    "cli.py",                     # prints where it was written
}


def test_only_allowed_modules_reference_ground_truth():
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "ground_truth" in p.read_text(encoding="utf-8").lower()
        and str(p.relative_to(SRC)) not in ALLOWED
    ]
    assert offenders == []
