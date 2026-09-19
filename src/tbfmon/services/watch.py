"""Directory watcher for `tbfmon watch` (polling; works over SSH, NFS and SMB shares).

A file is processed once it is complete: seen with the same size and mtime on two
consecutive polls. Processed files are recorded in SQLite (`watched_file`), so a
restart does not reprocess them; a file that changes later is processed again
(ingest is idempotent). A bad file becomes an error event, never a crash.

`FeedSimulator` writes drill-style reading files into the directory on a timer,
advancing a simulated clock — the "simulated live feed" for demos.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .drill import process_readings
from .drill_sim import INJECTABLE, next_drill_time, simulate_drill_rows, write_drill_csv
from .errors import TbfmonError
from .workspace import Workspace


@dataclass
class WatchEvent:
    file: str
    at: str
    ok: bool
    fans: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int = 0
    error: str | None = None
    hint: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DirectoryWatcher:
    def __init__(self, ws: Workspace, directory: Path, pattern: str = "*.csv"):
        if not directory.exists():
            raise TbfmonError(f"directory not found: {directory}", "create it or point `watch` at the drop folder")
        if not directory.is_dir():
            raise TbfmonError(f"{directory} is not a directory")
        self.ws = ws
        self.directory = directory
        self.pattern = pattern
        self._seen: dict[str, tuple[int, float]] = {}  # last poll's (size, mtime), for the "complete" test

    def _processed(self, path: Path) -> tuple[int, float] | None:
        r = self.ws.conn.execute("SELECT size, mtime FROM watched_file WHERE path = ?", (str(path.resolve()),)).fetchone()
        return (r[0], r[1]) if r else None

    def ready(self) -> list[Path]:
        """Complete files not yet processed in their current form."""
        out = []
        for p in sorted(self.directory.glob(self.pattern)):
            if not p.is_file() or "ground_truth" in p.name.lower():
                continue
            st = p.stat()
            sig = (st.st_size, st.st_mtime)
            prev, self._seen[str(p)] = self._seen.get(str(p)), sig
            if prev != sig or self._processed(p) == sig:
                continue
            out.append(p)
        return out

    def process(self, path: Path) -> WatchEvent:
        now = self.ws.settings.now().isoformat()
        st = path.stat()
        try:
            res = process_readings(self.ws, path, f"watch:{self.directory.name}")
            ev = WatchEvent(path.name, now, True, res.fans, res.alerts, res.exit_code, notes=res.notes)
        except TbfmonError as e:
            ev = WatchEvent(path.name, now, False, error=e.message, hint=e.hint, exit_code=0)
        self.ws.conn.execute("INSERT INTO watched_file(path, size, mtime, processed_at) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
                             "processed_at=excluded.processed_at",
                             (str(path.resolve()), st.st_size, st.st_mtime, now))
        self.ws.conn.commit()
        return ev

    def ready_peek(self) -> bool:
        """Any file present that has not been processed in its current form?"""
        for p in self.directory.glob(self.pattern):
            if p.is_file() and "ground_truth" not in p.name.lower():
                st = p.stat()
                if self._processed(p) != (st.st_size, st.st_mtime):
                    return True
        return False

    def poll(self) -> list[WatchEvent]:
        return [self.process(p) for p in self.ready()]

    def prime(self) -> None:
        """Record current sizes so files already complete are picked up on the next poll."""
        self.ready()


class FeedSimulator(threading.Thread):
    """Writes one simulated drill file per station every `every_s` seconds (daemon thread)."""

    def __init__(self, ws_factory, directory: Path, count: int = 4, every_s: float = 3.0, seed: int = 7,
                 fault_every: int = 2):
        super().__init__(daemon=True)
        self.ws_factory = ws_factory  # SQLite connections are per-thread
        self.directory = directory
        self.count, self.every_s, self.seed, self.fault_every = count, every_s, seed, fault_every
        self.stop_event = threading.Event()
        self.written: list[Path] = []
        self.error: str | None = None

    def run(self) -> None:
        try:
            ws = self.ws_factory()
            stations = ws.stations()
            when = next_drill_time(ws)
            for k in range(self.count):
                if self.stop_event.wait(self.every_s if k else 0.2):
                    return
                st = stations[k % len(stations)]
                fans = ws.fans_at(st)
                inject = {}
                if self.fault_every and (k + 1) % self.fault_every == 0 and fans:
                    inject = {fans[(k // self.fault_every) % len(fans)]: INJECTABLE[k % len(INJECTABLE)]}
                df = simulate_drill_rows(ws, fans, when, inject, self.seed + k)
                path = write_drill_csv(ws, st, df, when, self.directory)
                self.written.append(path)
                when += dt.timedelta(days=1)
        except Exception as e:  # noqa: BLE001 - surfaced by the caller
            self.error = str(e)
