"""Suspect-value rules (validate + condition views) and OEM PM due logic."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from tbfmon.models import MEASUREMENT_FIELDS
from tbfmon.pm import add_months, pm_status
from tbfmon.validate import find_isolated_spikes, suspect_points
from tbfmon.views import latest_readings


def _runs(values, p="vib_de_rms_mms", **extra):
    n = len(values)
    df = pd.DataFrame({
        "fan_id": "TBF-001",
        "ts": pd.date_range("2024-01-01", periods=n, freq="45D"),
        "duration_min": 20.0,
        "ambient_temp_c": 30.0,
    })
    for col in MEASUREMENT_FIELDS:
        if col != "ambient_temp_c":
            df[col] = np.nan
    df[p] = values
    for k, v in extra.items():
        df[k] = v
    return df


def test_isolated_spike_flagged_with_hint():
    df = _runs([1.2, 1.3, 1.25, 12.4, 1.2, 1.3])
    spikes = find_isolated_spikes(df)
    assert [(s["parameter"], s["value"], s["hint"]) for s in spikes] == [("vib_de_rms_mms", 12.4, "decimal shift ×10")]


def test_persistent_step_is_not_a_spike():
    # Degradation: the jump is confirmed by the following run.
    assert find_isolated_spikes(_runs([1.2, 1.3, 1.25, 4.0, 4.3, 4.6])) == []


def test_gradual_rise_never_suspect():
    df = _runs([1.2, 1.4, 1.7, 2.1, 2.6, 3.2, 3.9])
    assert suspect_points(df, "vib_de_rms_mms") == {}


def test_newest_jump_unconfirmed_and_excluded_from_condition():
    df = _runs([1.2, 1.3, 1.25, 1.28, 9.9])
    latest = latest_readings(df, dt.date(2030, 1, 1))["vib_de_rms_mms"]
    assert latest["suspect"]["kind"] == "unconfirmed" and latest["suspect"]["value"] == 9.9
    assert latest["value"] == 1.28  # condition uses the last trustworthy value


def test_repair_resets_history():
    df = _runs([1.2, 1.3, 1.25, 1.28, 6.0, 6.1])
    repair = df["ts"].iloc[4].date().isoformat()
    assert suspect_points(df, "vib_de_rms_mms", repairs=[repair]) == {}


def test_phase_inconsistency_and_stuck_zero():
    df = _runs([1.2, 0.0, 1.3], current_l1_a=[80.0, 80.0, 80.0], current_l2_a=[81.0, 500.0, 81.0],
               current_l3_a=[79.0, 79.0, 79.0])
    assert suspect_points(df, "current_l2_a")[1]["kind"] == "inconsistent"
    assert suspect_points(df, "vib_de_rms_mms")[1]["kind"] == "stuck_zero"


def test_add_months_clamps_day():
    assert add_months(dt.date(2024, 1, 31), 1) == dt.date(2024, 2, 29)
    assert add_months(dt.date(2024, 11, 15), 3) == dt.date(2025, 2, 15)


def test_pm_status():
    tasks = [
        {"task": "Regrease", "interval_months": 6, "interval_run_hours": 2.0, "needs_work_at_height": True},
        {"task": "IR test", "interval_months": 12, "interval_run_hours": None, "needs_work_at_height": False},
        {"task": "Balance", "interval_months": 24, "interval_run_hours": None, "needs_work_at_height": True},
    ]
    maint = [
        {"task": "Regrease", "type": "preventive", "date": "2025-06-01"},
        {"task": "IR test", "type": "preventive", "date": "2024-12-20"},
    ]
    runs = pd.DataFrame({"ts": pd.to_datetime(["2025-07-01", "2025-08-01"]), "duration_min": [90.0, 60.0]})
    st = {t["task"]: t for t in pm_status(tasks, maint, runs, dt.date(2025, 12, 1))}
    assert st["Regrease"]["status"] == "OVERDUE" and st["Regrease"]["run_hours_left"] == -0.5
    assert st["IR test"]["status"] == "DUE SOON" and st["IR test"]["next_due"] == "2025-12-20"
    assert st["Balance"]["status"] == "NO RECORD"
