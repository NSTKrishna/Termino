"""Workspace: database, limits, engine config and settings behind one object.

Friendly lookups (fans, stations) with suggestions, and a per-process cache of
engine assessments so several views of the same data do not re-run the engine.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
import sqlite3
from dataclasses import dataclass, field
from functools import cached_property

from pydantic import ValidationError

from .. import db
from ..engine import Assessment, EngineConfig, assess, load_engine_config, load_fleet
from ..limits import LimitsFile, load_limits
from ..paths import Home
from ..pm import resolve_as_of
from .errors import TbfmonError
from .settings import Settings, load_settings


@dataclass
class Workspace:
    home: Home
    conn: sqlite3.Connection
    _cache: dict = field(default_factory=dict, repr=False)

    @classmethod
    def open(cls, home: Home) -> "Workspace":
        try:
            conn = db.connect(home.db_path)
        except db.NotInitialized as e:
            raise TbfmonError(f"no workspace at {home.root}", "run `tbfmon init` (or pass --home / set TBFMON_HOME)") from e
        db.init_schema(conn)  # idempotent; migrates older workspaces (e.g. adds alert tables)
        return cls(home, conn)

    # ---- configuration ------------------------------------------------------------------------
    @cached_property
    def settings(self) -> Settings:
        return load_settings(self.home.root)

    @cached_property
    def limits(self) -> LimitsFile:
        try:
            return load_limits(self.home.limits_path)
        except (ValidationError, ValueError) as e:
            raise TbfmonError(f"limits.yaml is invalid: {str(e).splitlines()[0]}",
                              "run `tbfmon validate` or `tbfmon init --force` to restore defaults") from e

    @cached_property
    def engine_cfg(self) -> EngineConfig:
        try:
            return load_engine_config(self.home.engine_config_path)
        except (ValidationError, ValueError) as e:
            raise TbfmonError(f"engine.yaml is invalid: {str(e).splitlines()[0]}",
                              "fix it or run `tbfmon init --force` to restore defaults") from e

    # ---- lookups ------------------------------------------------------------------------------
    def fan_ids(self) -> list[str]:
        return sorted(db.fan_ids(self.conn))

    def fan_id(self, raw: str) -> str:
        ids = self.fan_ids()
        if not ids:
            raise TbfmonError("no fans in the database", "ingest data first (`tbfmon ingest` or `tbfmon simulate`)")
        cand = raw.strip().upper()
        if cand in ids:
            return cand
        cand = re.sub(r"(?<=\d)O|O(?=\d)", "0", cand)  # letter O typed for zero: TBF-0O5
        if cand in ids:
            return cand
        m = re.fullmatch(r"(?:TBF)?[-_ ]?0*(\d+)", cand)  # "7", "tbf-40", "TBF7" → TBF-007 / TBF-040
        if m:
            num = f"TBF-{int(m.group(1)):03d}"
            if num in ids:
                return num
        close = difflib.get_close_matches(cand, ids, n=3, cutoff=0.6)
        raise TbfmonError(f"unknown fan {raw!r}",
                          f"did you mean {', '.join(close)}?" if close else "see `tbfmon fleet` for fan IDs")

    def stations(self) -> list[str]:
        return sorted({f["station"] for f in db.list_fans(self.conn)})

    def station(self, raw: str) -> str:
        stations = self.stations()
        exact = [s for s in stations if s.lower() == raw.strip().lower()]
        if exact:
            return exact[0]
        partial = [s for s in stations if raw.strip().lower() in s.lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise TbfmonError(f"station {raw!r} is ambiguous", f"matches: {', '.join(partial)}")
        close = difflib.get_close_matches(raw.strip().title(), stations, n=3, cutoff=0.5)
        raise TbfmonError(f"unknown station {raw!r}",
                          f"did you mean {', '.join(close)}?" if close else f"stations: {', '.join(stations)}")

    def fans_at(self, station: str) -> list[str]:
        return sorted(f["fan_id"] for f in db.list_fans(self.conn) if f["station"] == station)

    # ---- engine -------------------------------------------------------------------------------
    def as_of(self, spec: str | dt.date | None = None) -> dt.date:
        if isinstance(spec, dt.date):
            return spec
        try:
            return resolve_as_of(self.conn, spec)[0]
        except ValueError as e:
            raise TbfmonError(f"bad --as-of {spec!r}", "use YYYY-MM-DD, 'today' or 'data'") from e

    def assessment(self, as_of: str | dt.date | None = None) -> Assessment:
        ref = self.as_of(as_of)
        if ref not in self._cache:
            data = load_fleet(self.conn, self.limits, ref)
            if data.runs.empty:
                raise TbfmonError("no test runs in the database", "ingest data first (`tbfmon ingest` or `tbfmon simulate`)")
            self._cache[ref] = assess(data, self.engine_cfg, "full")
        return self._cache[ref]

    def invalidate(self) -> None:
        """Call after new data is ingested."""
        self._cache.clear()
