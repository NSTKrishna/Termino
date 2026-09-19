"""Simulated mock-drill readings for `tbfmon drill --simulate`, `watch --simulate-feed` and the demo.

Each fan's reading is its recent median (last 5 measured values per parameter) with
instrument noise; optional injected faults push the characteristic parameters beyond
their permissible limits so the drill has something to find. The simulated clock
follows the data: a drill is placed the night after the latest run in the database.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

from ..models import MEASUREMENT_FIELDS
from .errors import TbfmonError
from .workspace import Workspace

INJECTABLE = ("bearing_wear", "impeller_imbalance", "misalignment_looseness", "insulation_degradation",
              "airflow_restriction", "lubrication_loss")
# Relative instrument noise per parameter (phases share a common load term, see below).
NOISE = {"vib": 0.08, "temp": 0.02, "voltage_v": 0.008, "rpm": 0.001, "airflow_m3s": 0.03,
         "diff_pressure_pa": 0.03, "noise_dba": 0.008, "insulation_resistance_mohm": 0.12,
         "phase": 0.003, "load": 0.01, "default": 0.03}


def next_drill_time(ws: Workspace, after: dt.datetime | None = None) -> dt.datetime:
    row = ws.conn.execute("SELECT MAX(ts) FROM test_run").fetchone()
    last = dt.datetime.fromisoformat(row[0]) if row and row[0] else dt.datetime(2026, 1, 1)
    base = max(last, after) if after else last
    return dt.datetime.combine(base.date() + dt.timedelta(days=1), dt.time(2, 0))


def parse_inject(spec: list[str] | None, ws: Workspace) -> dict[str, str]:
    """['TBF-030:bearing_wear', '32:insulation_degradation'] → {fan: mode}."""
    out: dict[str, str] = {}
    for item in spec or []:
        if ":" not in item:
            raise TbfmonError(f"bad --inject {item!r}", "use FAN:MODE, e.g. TBF-030:bearing_wear")
        fan, mode = item.split(":", 1)
        mode = mode.strip().lower()
        if mode not in INJECTABLE:
            raise TbfmonError(f"unknown fault mode {mode!r}", f"use one of {', '.join(INJECTABLE)}")
        out[ws.fan_id(fan)] = mode
    return out


def _recent_medians(ws: Workspace, fan_id: str) -> dict[str, float]:
    df = pd.read_sql_query("SELECT * FROM test_run WHERE fan_id = ? ORDER BY ts DESC LIMIT 40", ws.conn, params=(fan_id,))
    med = {}
    for p in MEASUREMENT_FIELDS:
        s = pd.to_numeric(df[p], errors="coerce").dropna()
        s = s[s > 0] if p != "ambient_temp_c" else s
        if len(s):
            med[p] = float(s.head(5).median())
    return med


def _apply_fault(v: dict[str, float], mode: str) -> None:
    def rms(pos: str) -> None:  # keep overall RMS consistent with its components
        c1, c2 = v.get(f"vib_{pos}_1x_mms", 0.0), v.get(f"vib_{pos}_2x_mms", 0.0)
        bb = max(v.get(f"vib_{pos}_rms_mms", 0.0) ** 2 - c1**2 - c2**2, 0.1)
        v[f"vib_{pos}_rms_mms"] = math.sqrt(c1**2 + c2**2 + bb)

    if mode == "bearing_wear":
        v["vib_de_rms_mms"] = v.get("vib_de_rms_mms", 1.2) + 6.2
        v["temp_bearing_de_c"] = v.get("temp_bearing_de_c", 50) + 14
        v["noise_dba"] = v.get("noise_dba", 88) + 6
        v["coastdown_time_s"] = v.get("coastdown_time_s", 80) * 0.8
    elif mode == "impeller_imbalance":
        v["vib_de_1x_mms"] = v.get("vib_de_1x_mms", 0.9) + 5.5
        v["vib_nde_1x_mms"] = v.get("vib_nde_1x_mms", 0.7) + 4.0
        rms("de"), rms("nde")
    elif mode == "misalignment_looseness":
        v["vib_de_2x_mms"] = v.get("vib_de_2x_mms", 0.35) + 4.5
        v["vib_de_1x_mms"] = v.get("vib_de_1x_mms", 0.9) + 1.2
        rms("de")
        v["temp_bearing_de_c"] = v.get("temp_bearing_de_c", 50) + 6
    elif mode == "insulation_degradation":
        v["insulation_resistance_mohm"] = 4.0
        for p, f in (("current_l1_a", 1.07), ("current_l2_a", 0.965), ("current_l3_a", 0.965)):
            if p in v:
                v[p] *= f
        v["temp_winding_c"] = v.get("temp_winding_c", 80) + 12
    elif mode == "airflow_restriction":
        for p, f in (("airflow_m3s", 0.72), ("diff_pressure_pa", 1.35), ("current_l1_a", 0.93),
                     ("current_l2_a", 0.93), ("current_l3_a", 0.93)):
            if p in v:
                v[p] *= f
        v["noise_dba"] = v.get("noise_dba", 88) + 4
    elif mode == "lubrication_loss":
        v["temp_bearing_de_c"] = v.get("temp_bearing_de_c", 50) + 38
        v["noise_dba"] = v.get("noise_dba", 88) + 5
        v["coastdown_time_s"] = v.get("coastdown_time_s", 80) * 0.75


def simulate_drill_rows(ws: Workspace, fan_ids: list[str], when: dt.datetime,
                        inject: dict[str, str] | None = None, seed: int | None = None) -> pd.DataFrame:
    inject = inject or {}
    rng = np.random.default_rng(seed if seed is not None else zlib.crc32(f"{fan_ids}{when}".encode()))
    rows = []
    for k, fan_id in enumerate(fan_ids):
        med = _recent_medians(ws, fan_id)
        if not med:
            continue
        v = {}
        load = math.exp(rng.normal(0, NOISE["load"]))  # the three phases move together with load
        for p, m in med.items():
            if p == "ambient_temp_c":
                v[p] = m + rng.normal(0, 0.5)
                continue
            if p.startswith("current_l"):
                v[p] = m * load * math.exp(rng.normal(0, NOISE["phase"]))
                continue
            sd = NOISE["vib"] if p.startswith("vib_") else NOISE["temp"] if "temp" in p else NOISE.get(p, NOISE["default"])
            v[p] = m * math.exp(rng.normal(0, sd))
        if fan_id in inject:
            _apply_fault(v, inject[fan_id])
        v.pop("current_imbalance_pct", None)  # recomputed from phases at ingest
        rows.append({"fan_id": fan_id, "timestamp": (when + dt.timedelta(minutes=12 * k)).isoformat(),
                     "run_type": "mock_drill", "duration_min": 30.0,
                     **{p: round(x, 2) for p, x in v.items()}})
    return pd.DataFrame(rows, columns=["fan_id", "timestamp", "run_type", "duration_min", *MEASUREMENT_FIELDS])


def write_drill_csv(ws: Workspace, station: str, df: pd.DataFrame, when: dt.datetime,
                    directory: Path | None = None) -> Path:
    directory = directory or ws.home.drills_dir
    directory.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", station.lower()).strip("-")
    path = directory / f"drill_{slug}_{when:%Y%m%dT%H%M}.csv"
    df.to_csv(path, index=False, lineterminator="\n")
    return path
