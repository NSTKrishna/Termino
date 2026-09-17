"""Simulator: seed determinism and realism properties."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
import pytest

from tbfmon.models import MEASUREMENT_FIELDS, FailureRecord
from tbfmon.simulate import BUNDLE_FILES, GROUND_TRUTH_FILE, generate


@pytest.fixture(scope="module")
def ds():
    return generate(n_fans=40, years=3, seed=42)


def _digest(paths) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_same_seed_byte_identical_files(tmp_path):
    a = generate(12, 2, seed=7).write(tmp_path / "a" / "sim", tmp_path / "a" / "gt")
    b = generate(12, 2, seed=7).write(tmp_path / "b" / "sim", tmp_path / "b" / "gt")
    assert _digest(a.values()) == _digest(b.values())


def test_different_seed_differs():
    a, b = generate(12, 2, seed=7), generate(12, 2, seed=8)
    assert not a.test_runs.equals(b.test_runs)


def test_fleet_prefix_stable_across_fleet_size():
    # Per-fan random streams: adding fans must not change existing fans' nameplates.
    small, big = generate(8, 1, seed=3), generate(12, 1, seed=3)
    pd.testing.assert_frame_equal(small.fans, big.fans.iloc[:8])


def test_ground_truth_written_outside_bundle(tmp_path, ds):
    out = ds.write(tmp_path / "sim", tmp_path / "ground_truth")
    assert sorted(p.name for p in (tmp_path / "sim").iterdir()) == sorted(BUNDLE_FILES.values())
    assert out["ground_truth"].name == GROUND_TRUTH_FILE
    assert out["ground_truth"].parent != tmp_path / "sim"


def test_sparse_irregular_sampling(ds):
    runs = ds.test_runs[ds.test_runs.fan_id.isin(ds.fans.fan_id)]
    per_year = runs.groupby("fan_id").size() / 3
    assert per_year.min() >= 3.5 and per_year.max() <= 13.5
    gaps = pd.to_datetime(runs.timestamp).groupby(runs.fan_id).diff().dt.days.dropna()
    assert gaps.std() > 20  # irregular, not a fixed interval
    assert set(runs.run_type) == {"mock_drill", "scheduled_test", "commissioning"}


def test_defect_rates(ds):
    vals = ds.test_runs[list(MEASUREMENT_FIELDS)].to_numpy(dtype=float)
    sentinel = np.isin(vals, [-999.0, 9999.0]).sum()
    assert sentinel > 0
    assert ds.test_runs.duplicated().sum() >= 1
    assert (~ds.test_runs.fan_id.isin(ds.fans.fan_id)).sum() >= 1  # transcription typos
    # Overall missingness is dominated by instrument availability, but no column is empty.
    assert ds.test_runs[list(MEASUREMENT_FIELDS)].notna().mean().min() > 0.2


def test_failures_match_ground_truth_and_have_symptoms(ds):
    gt = ds.ground_truth
    failed = gt[gt.outcome == "failed"]
    assert len(failed) == len(ds.failures) > 0
    merged = failed.merge(ds.failures, left_on=["fan_id", "failure_date", "fault_mode"],
                          right_on=["fan_id", "date", "failure_mode"], how="left", indicator=True)
    assert (merged["_merge"] == "both").all()
    for row in ds.failures.to_dict("records"):
        rec = FailureRecord.model_validate({**row, "symptoms": row["symptoms"] if isinstance(row["symptoms"], str) else ""})
        if rec.failure_mode.value != "other":
            assert rec.symptoms, f"no symptoms for {row}"


def test_failure_followed_by_corrective_and_commissioning(ds):
    m, runs = ds.maintenance, ds.test_runs
    for f in ds.failures.to_dict("records"):
        after = m[(m.fan_id == f["fan_id"]) & (m.date >= f["date"]) & (m.type == "corrective")]
        assert len(after) >= 1
        repair = after.date.min()
        comm = runs[(runs.fan_id == f["fan_id"]) & (runs.run_type == "commissioning")
                    & (runs.timestamp.str[:10] == repair)]
        assert len(comm) >= 1


def test_bearing_wear_raises_vibration_before_failure(ds):
    gt = ds.ground_truth
    bw = gt[(gt.fault_mode == "bearing_wear") & (gt.outcome == "failed")]
    assert len(bw)
    rose = 0
    for ep in bw.to_dict("records"):
        col = f"vib_{ep['location'].lower()}_rms_mms"
        r = ds.test_runs[(ds.test_runs.fan_id == ep["fan_id"])].copy()
        r = r[(r[col] > 0) & (r[col] < 50)]
        before_onset = r[r.timestamp < ep["onset_date"]][col].median()
        last = r[(r.timestamp < ep["failure_date"])][col].tail(2).median()
        if pd.notna(before_onset) and pd.notna(last) and last > 1.5 * before_onset:
            rose += 1
    assert rose >= max(1, len(bw) // 2)


def test_pm_schedule_has_work_at_height(ds):
    assert ds.pm_schedule.needs_work_at_height.any() and (~ds.pm_schedule.needs_work_at_height).any()
    assert (ds.maintenance.type == "preventive").sum() > (ds.maintenance.type == "corrective").sum()
