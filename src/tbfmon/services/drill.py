"""Mock-drill mode: ingest a station's test runs, analyse each fan, raise alerts, rank by risk.

Also the shared "reading file" processing used by `watch`: validate the CSV, ingest
the rows, analyse exactly the runs that arrived and raise alerts for them.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from ..ingest import ingest
from ..models import MEASUREMENT_FIELDS
from ..sources.base import RawRecord
from .alerts import AlertStore, candidates_from_analysis
from .analysis import STATUS_ORDER, _build
from .drill_sim import next_drill_time, parse_inject, simulate_drill_rows, write_drill_csv
from .errors import SEVERITY_EXIT, STATUS_EXIT, TbfmonError
from .workspace import Workspace

REQUIRED = ("fan_id", "timestamp", "run_type", "duration_min")


class RowsSource:
    """In-memory Source (tbfmon.sources protocol) for rows already read and checked."""

    def __init__(self, name: str, rows: list[tuple[str, dict]]):
        self.name = name
        self._rows = rows

    def records(self) -> Iterator[RawRecord]:
        for origin, data in self._rows:
            yield RawRecord("test_run", data, origin)


def _clean(v: Any) -> Any:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    v = str(v).strip()
    return v or None


def read_readings(path: Path) -> pd.DataFrame:
    """Read and sanity-check a test-run CSV; friendly errors for the usual mistakes."""
    if not path.exists():
        raise TbfmonError(f"file not found: {path}")
    if path.suffix.lower() != ".csv":
        raise TbfmonError(f"{path.name} is not a .csv file", "export the readings as CSV (see README: 'Your own data')")
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError) as e:
        raise TbfmonError(f"cannot read {path.name}: {str(e).splitlines()[0]}", "expected a comma-separated UTF-8 CSV") from e
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise TbfmonError(f"{path.name} is missing column(s): {', '.join(missing)}",
                          f"required: {', '.join(REQUIRED)}; measurements: {', '.join(MEASUREMENT_FIELDS[:4])}, … "
                          "(DESIGN.md §3)")
    if df.empty:
        raise TbfmonError(f"{path.name} has no rows")
    if not any(c in df.columns for c in MEASUREMENT_FIELDS):
        raise TbfmonError(f"{path.name} has no measurement columns", "e.g. vib_de_rms_mms, temp_bearing_de_c, current_l1_a")
    return df


@dataclass
class ReadingsResult:
    source: str
    file: str
    ingest: dict[str, Any]
    skipped: list[dict[str, str]]
    unknown_columns: list[str]
    fans: list[dict[str, Any]]           # FanAnalysis dicts, risk-sorted
    alerts: list[dict[str, Any]]         # AlertOutcome dicts
    exit_code: int
    station: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def process_readings(ws: Workspace, path: Path, source: str, station: str | None = None) -> ReadingsResult:
    df = read_readings(path)
    fans_at = set(ws.fans_at(station)) if station else None
    known = set(ws.fan_ids())
    rows, skipped = [], []
    for k, rec in enumerate(df.to_dict("records")):
        data = {c: _clean(v) for c, v in rec.items()}
        origin = f"{path.name}:{k + 2}"
        fid = (data.get("fan_id") or "").upper()
        if fid not in known:
            try:
                fid = ws.fan_id(fid)  # tolerate "12", "tbf-12", letter-O typos
            except TbfmonError as e:
                skipped.append({"row": origin, "reason": f"{e.message} ({e.hint})"})
                continue
        if fans_at is not None and fid not in fans_at:
            skipped.append({"row": origin, "reason": f"{fid} is not at {station}"})
            continue
        data["fan_id"] = fid
        rows.append((origin, data))
    if not rows:
        raise TbfmonError(f"no usable rows in {path.name}",
                          "; ".join(s["reason"] for s in skipped[:3]) or "check fan IDs and station")
    report = ingest(ws.conn, RowsSource(f"{source}:{path.name}", rows))
    ws.invalidate()
    run_keys = []
    for _, d in rows:
        try:
            ts = pd.Timestamp(d["timestamp"]).isoformat()
        except (ValueError, TypeError):
            continue
        r = ws.conn.execute("SELECT run_id FROM test_run WHERE fan_id=? AND ts=? AND run_type=?",
                            (d["fan_id"], ts, d.get("run_type"))).fetchone()
        if r:
            run_keys.append((d["fan_id"], r[0], ts))
    if not run_keys:
        raise TbfmonError(f"none of the rows in {path.name} could be stored",
                          "; ".join(f"{x['field']}: {x['issue']}" for x in report.rejected_examples[:3]))
    analyses = analyze_runs(ws, run_keys)
    cands = [c for an in analyses for c in candidates_from_analysis(an, source)]
    outcomes = [o.to_dict() for o in AlertStore(ws).raise_many(cands)]
    code = max([STATUS_EXIT[a["status"]] for a in analyses]
               + [SEVERITY_EXIT[o["severity"]] for o in outcomes if o["outcome"] != "suppressed"], default=0)
    unknown = sorted(set(df.columns) - set(REQUIRED) - set(MEASUREMENT_FIELDS) - {"current_imbalance_pct"})
    notes = [f"{len(skipped)} row(s) skipped"] if skipped else []
    if report.values_nulled:
        notes.append(f"{report.values_nulled} impossible value(s) nulled at ingest (sentinel/out of range)")
    return ReadingsResult(source, str(path), report.to_dict(), skipped, unknown, analyses, outcomes, code,
                          station, notes)


def analyze_runs(ws: Workspace, run_keys: list[tuple[str, int, str]]) -> list[dict[str, Any]]:
    """Analyse specific runs (one shared, point-in-time assessment), riskiest first."""
    as_of = max(dt.date.fromisoformat(ts[:10]) for _, _, ts in run_keys)
    a = ws.assessment(as_of)
    runs = a.data.runs
    out = []
    for fan_id, run_id, _ in run_keys:
        idx = runs.index[runs["run_id"] == run_id]
        if len(idx):
            out.append(_build(ws, a, int(idx[0])).to_dict())
    out.sort(key=lambda d: (STATUS_ORDER[d["status"]], d["health_index"], d["fan_id"]))
    return out


def run_drill(ws: Workspace, station: str, file: Path | None = None, simulate: bool = False,
              inject: list[str] | None = None, seed: int | None = None) -> ReadingsResult:
    if bool(file) == bool(simulate):
        raise TbfmonError("give exactly one of --file CSV or --simulate")
    st = ws.station(station)
    if simulate:
        fans = ws.fans_at(st)
        when = next_drill_time(ws)
        df = simulate_drill_rows(ws, fans, when, parse_inject(inject, ws), seed)
        file = write_drill_csv(ws, st, df, when)
    elif inject:
        raise TbfmonError("--inject only applies to --simulate")
    res = process_readings(ws, Path(file), f"drill:{st}", station=st)
    res.station = st
    drilled = {a["fan_id"] for a in res.fans}
    not_run = [f for f in ws.fans_at(st) if f not in drilled]
    if not_run:
        res.notes.append(f"no drill reading for: {', '.join(not_run)} — fans that did not run cannot be assessed")
    return res
