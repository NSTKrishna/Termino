"""Workspace creation and simulated-fleet loading (shared by `init`, `simulate` and `demo`)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .. import db
from ..engine.config import default_engine_text
from ..ingest import IngestReport, ingest
from ..limits import default_limits_text
from ..paths import Home
from .settings import DEFAULT_CONFIG


def init_workspace(home: Home, force: bool = False) -> dict[str, Any]:
    home.root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(home.db_path, create=True)
    db.init_schema(conn)
    written = {}
    for key, path, text in (("limits", home.limits_path, default_limits_text()),
                            ("config", home.config_path, DEFAULT_CONFIG),
                            ("engine_config", home.engine_config_path, default_engine_text())):
        written[key] = force or not path.exists()
        if written[key]:
            path.write_text(text, encoding="utf-8")
    res = {"home": str(home.root), "db": str(home.db_path), "limits": str(home.limits_path),
           "limits_written": written["limits"], "engine_config": str(home.engine_config_path),
           "engine_config_written": written["engine_config"], "config": str(home.config_path),
           "config_written": written["config"], "schema_version": db.SCHEMA_VERSION, "counts": db.counts(conn)}
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
