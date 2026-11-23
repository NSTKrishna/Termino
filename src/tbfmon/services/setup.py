"""Workspace creation and simulated-fleet loading (shared by `init` and `simulate`)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .. import db
from ..ingest import IngestReport, ingest
from ..limits import default_limits_text
from ..paths import Home


def init_workspace(home: Home, force: bool = False) -> dict[str, Any]:
    home.root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(home.db_path, create=True)
    db.init_schema(conn)
    limits_written = force or not home.limits_path.exists()
    if limits_written:
        home.limits_path.write_text(default_limits_text(), encoding="utf-8")
    res = {"home": str(home.root), "db": str(home.db_path), "limits": str(home.limits_path),
           "limits_written": limits_written, "schema_version": db.SCHEMA_VERSION, "counts": db.counts(conn)}
    conn.close()
    return res


def simulate_into(home: Home, fans: int, years: int, seed: int, start: dt.date,
                  ingest_data: bool = True) -> tuple[Any, dict[str, Any], IngestReport | None]:
    """Generate a synthetic fleet, write the CSV bundle + ground truth, optionally ingest it.
    Returns (dataset, files, ingest report)."""
    from ..simulate import generate
    from ..sources import SimulatorSource

    ds = generate(n_fans=fans, years=years, seed=seed, start=start)
    files = ds.write(home.sim_dir, home.ground_truth_dir)
    report = None
    if ingest_data:
        conn = db.connect(home.db_path)
        report = ingest(conn, SimulatorSource(ds))
        conn.close()
    return ds, files, report
