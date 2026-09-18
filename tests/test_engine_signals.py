"""Engine layers 0–3: config, cleaning, limit check, features, baseline/drift/peers, segments."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from tbfmon.engine.baseline import baseline_and_drift, last_known, peer_z
from tbfmon.engine.clean import clean_runs
from tbfmon.engine.config import CleaningCfg, EngineConfig, FeaturesCfg, default_engine_text
from tbfmon.engine.data import segment_ids
from tbfmon.engine.features import FEATURE_NAMES, GROUPS, compute_features
from tbfmon.engine.limit_check import check_run, worst_status
from tbfmon.limits import load_limits
from tbfmon.models import MEASUREMENT_FIELDS

FAN = {"fan_id": "F1", "capacity_class": "C45", "rated_power_kw": 45.0, "rated_airflow_m3s": 35.0,
       "rated_diff_pressure_pa": 600.0, "rated_rpm": 1480.0, "rated_voltage_v": 415.0, "rated_current_a": 80.0}


def runs_frame(n: int, fan_id: str = "F1", **cols) -> pd.DataFrame:
    df = pd.DataFrame({"fan_id": fan_id, "ts": pd.date_range("2024-01-01", periods=n, freq="30D"),
                       "run_type": "scheduled_test", "duration_min": 30.0})
    for m in MEASUREMENT_FIELDS:
        df[m] = np.nan
    df["ambient_temp_c"] = 30.0
    for k, v in cols.items():
        df[k] = v
    df["segment"] = 0
    df["seg_none"] = 0
    for g in GROUPS:
        df[f"seg_{g}"] = 0
    return df


def fans_frame(*ids: str, cls: str = "C45") -> pd.DataFrame:
    return pd.DataFrame([{**FAN, "fan_id": i, "capacity_class": cls} for i in ids]).set_index("fan_id")


# ---- config -------------------------------------------------------------------------------

def test_default_engine_config_loads_and_weights_sum_to_100(engine_cfg):
    assert sum(engine_cfg.health_index.weights.values()) == 100
    data = yaml.safe_load(default_engine_text())
    data["health_index"]["weights"]["limits"] = 50
    with pytest.raises(ValidationError):
        EngineConfig.model_validate(data)


# ---- layer 0: causal cleaning ----------------------------------------------------------------

def test_clean_flags_zero_phase_and_unconfirmed_jump():
    df = runs_frame(6, vib_de_rms_mms=[1.2, 1.3, 1.25, 0.0, 1.2, 9.0],
                    current_l1_a=[70.0] * 6, current_l2_a=[71, 71, 71, 71, 300, 71], current_l3_a=[69.0] * 6)
    clean, sus = clean_runs(df, CleaningCfg())
    assert sus[3]["vib_de_rms_mms"] == "stuck_zero"
    assert sus[4]["current_l2_a"] == "inconsistent"
    assert sus[5]["vib_de_rms_mms"] == "unconfirmed"
    assert np.isnan(clean.loc[5, "vib_de_rms_mms"]) and clean.loc[4, "vib_de_rms_mms"] == 1.2


def test_clean_accepts_confirmed_step():
    df = runs_frame(6, vib_de_rms_mms=[1.2, 1.3, 1.25, 4.0, 4.1, 4.2])
    _, sus = clean_runs(df, CleaningCfg())
    assert sus.get(3, {}).get("vib_de_rms_mms") == "unconfirmed"  # judged causally at its own run
    assert "vib_de_rms_mms" not in sus.get(4, {}) and "vib_de_rms_mms" not in sus.get(5, {})


# ---- layer 1: limit check --------------------------------------------------------------------

def test_limit_check_status_and_pct_deviation():
    lims = load_limits().for_class("C45")
    run = {"vib_de_rms_mms": 5.4, "insulation_resistance_mohm": 50.0, "current_l1_a": 60.0, "ambient_temp_c": 30.0}
    res = {r.parameter: r for r in check_run(run, FAN, lims)}
    assert res["vib_de_rms_mms"].status == "WARN" and res["vib_de_rms_mms"].pct_deviation == pytest.approx(20.0)
    assert res["insulation_resistance_mohm"].side == "low"
    assert res["insulation_resistance_mohm"].pct_deviation == pytest.approx(50.0)  # (100 − 50)/100
    assert res["current_l1_a"].status == "OK" and res["current_l1_a"].pct_deviation < 0
    suspect = check_run({**run, "vib_de_rms_mms": 9.0}, FAN, lims, {"vib_de_rms_mms": "unconfirmed"})
    assert worst_status(suspect) == "WARN"  # the suspect ALARM does not count


# ---- layer 2: features -----------------------------------------------------------------------

def test_feature_normalisations():
    df = runs_frame(2, temp_bearing_de_c=[50.0, 50.0], insulation_resistance_mohm=[100.0, 100.0],
                    airflow_m3s=[33.0, 33.0], rpm=[1465.0, np.nan], vib_de_rms_mms=[5.0, 5.0],
                    vib_de_1x_mms=[3.0, 3.0], vib_de_2x_mms=[0.0, 0.0])
    df["duration_min"] = [25.0, 10.0]
    f = compute_features(df, fans_frame("F1"), FeaturesCfg())
    assert f.loc[0, "temp_de_rise"] == pytest.approx(20.0 / (1 - math.exp(-1)))
    assert f.loc[1, "temp_de_rise"] > f.loc[0, "temp_de_rise"]  # shorter run ⇒ larger steady-state estimate
    assert f.loc[0, "ir40_log"] == pytest.approx(math.log10(50.0))  # 100 MΩ @30 °C ≈ 50 MΩ @40 °C
    assert f.loc[0, "airflow_pct"] == pytest.approx(33.0 / (1465 / 1480) / 35 * 100)
    assert f.loc[1, "airflow_pct"] == pytest.approx(33.0 / 35 * 100)  # no rpm ⇒ rated speed assumed
    assert f.loc[0, "vib_de_bb"] == pytest.approx(4.0)  # √(5² − 3² − 0²)
    assert f.loc[1, "d_ir40_log"] == pytest.approx(0.0) and np.isnan(f.loc[0, "d_ir40_log"])


# ---- layer 3: baseline, drift, peers ---------------------------------------------------------

def _drift(values):
    df = runs_frame(len(values), vib_de_rms_mms=values)
    f = compute_features(df, fans_frame("F1"), FeaturesCfg())
    from tbfmon.engine.config import BaselineCfg, DriftCfg
    return baseline_and_drift(f, BaselineCfg(), DriftCfg())


def test_baseline_needs_min_runs_then_z():
    z = _drift([1.0, 1.1, 0.9, 1.0, 1.05, 1.0])
    assert z["z_vib_de_rms"].iloc[:2].isna().all() and z["z_vib_de_rms"].iloc[2:].notna().all()
    assert not z["baseline_established"].iloc[0] and z["baseline_established"].iloc[-1]


def test_step_change_triggers_drift_but_noise_does_not():
    noisy = _drift([1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.1, 1.0, 0.92, 1.08])
    assert not noisy["drift_vib_de_rms"].any()
    step = _drift([1.0, 1.1, 0.9, 1.0, 1.05, 2.0, 2.1, 2.2, 2.3, 2.4])
    assert step["drift_vib_de_rms"].iloc[-1] and step["z_vib_de_rms"].iloc[-1] > 3
    assert step["cusum_vib_de_rms"].iloc[-1] > step["cusum_vib_de_rms"].iloc[5]


def test_peer_z_leave_one_out(engine_cfg):
    frames = []
    for k, fid in enumerate(["A", "B", "C", "D", "E"]):
        level = 4.0 if fid == "E" else 1.0 + 0.05 * k
        frames.append(runs_frame(14, fan_id=fid, vib_de_rms_mms=[level] * 14))
    df = pd.concat(frames, ignore_index=True)
    fans = fans_frame("A", "B", "C", "D", "E")
    f = compute_features(df, fans, FeaturesCfg())
    pz = peer_z(f, fans, engine_cfg.peers, engine_cfg.drift)
    late = f["ts"] > "2024-06-01"
    assert pz.loc[late & (f.fan_id == "E"), "pzb_vib_de_rms"].min() > 5
    assert pz.loc[late & (f.fan_id == "A"), "pzb_vib_de_rms"].abs().max() < 3


def test_last_known_carries_sparse_values_with_age_limit():
    df = runs_frame(5, insulation_resistance_mohm=[1000.0, np.nan, np.nan, np.nan, np.nan])
    df["ts"] = pd.to_datetime(["2024-01-01", "2024-02-01", "2024-04-01", "2024-06-01", "2024-09-01"])
    f = compute_features(df, fans_frame("F1"), FeaturesCfg())
    k = last_known(f, max_age_days=180)
    assert k["ir40_log"].iloc[:4].notna().all() and np.isnan(k["ir40_log"].iloc[4])


def test_repair_resets_only_related_feature_groups():
    runs = runs_frame(4)
    maint = pd.DataFrame({"fan_id": ["F1", "F1"], "date": pd.to_datetime(["2024-02-15", "2024-03-20"]),
                          "type": ["corrective", "corrective"]})
    fails = pd.DataFrame({"fan_id": ["F1"], "date": pd.to_datetime(["2024-02-10"]),
                          "failure_mode": ["bearing_wear"]})
    seg = segment_ids(runs, maint, fails)
    assert seg["seg_vib"].tolist() == [0, 0, 1, 2]     # bearing repair + unexplained repair
    assert seg["seg_aero"].tolist() == [0, 0, 0, 1]    # only the unexplained repair resets airflow
    assert seg["segment"].tolist() == [0, 0, 1, 2]


def test_every_feature_computed_on_simulated_fleet(sim_fleet, engine_cfg):
    clean, _ = clean_runs(sim_fleet.runs, engine_cfg.cleaning)
    f = compute_features(clean, sim_fleet.fans, engine_cfg.features)
    assert set(FEATURE_NAMES) <= set(f.columns)
    assert all(f[n].notna().any() for n in FEATURE_NAMES)
