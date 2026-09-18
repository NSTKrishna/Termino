"""Layer 2 — per-run condition features (normalised, explainable).

Normalisations (DESIGN.md §9.2):
* temperatures → rise over ambient, duration-normalised to an estimated steady
  state: (T − ambient) / (1 − e^(−t/τ)); runs too short to say anything are NaN
* insulation resistance → corrected to 40 °C (halves per +10 °C, IEEE 43),
  measured cold so ambient ≈ winding temperature; log10
* airflow ∝ rpm and ΔP ∝ rpm² (fan laws) → % of rating at rated speed
* currents → % of rated current; inrush → multiple of rated current
* vibration broadband = √(RMS² − 1ײ − 2ײ): the part not explained by 1×/2×,
  which is where bearing defects show. Speed varies <1 % between runs, so
  vibration is not speed-normalised.
* deltas vs the previous run of the same segment for every feature

Segments are per feature group: a repair resets only the groups it physically
changes (MODE_RESETS), e.g. a bearing replacement does not reset airflow.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import FeaturesCfg

MIN_THERMAL_FRACTION = 0.2  # runs shorter than ~0.2 τ carry too little thermal information


@dataclass(frozen=True)
class Feature:
    name: str
    label: str
    unit: str
    bad: int          # +1 higher is worse, −1 lower is worse, 0 either way
    abs_floor: float  # minimum robust scale (noise floor) in feature units
    rel_floor: float  # minimum robust scale as a fraction of the baseline median
    key: bool = True  # used by drift, peers and the health index


FEATURES: dict[str, Feature] = {f.name: f for f in [
    Feature("vib_de_rms", "Vib DE RMS", "mm/s", +1, 0.25, 0.12),
    Feature("vib_nde_rms", "Vib NDE RMS", "mm/s", +1, 0.25, 0.12),
    Feature("vib_de_1x", "Vib DE 1×", "mm/s", +1, 0.2, 0.12),
    Feature("vib_nde_1x", "Vib NDE 1×", "mm/s", +1, 0.2, 0.12),
    Feature("vib_de_2x", "Vib DE 2×", "mm/s", +1, 0.15, 0.15),
    Feature("vib_nde_2x", "Vib NDE 2×", "mm/s", +1, 0.15, 0.15),
    Feature("vib_de_bb", "Vib DE broadband", "mm/s", +1, 0.25, 0.15),
    Feature("vib_nde_bb", "Vib NDE broadband", "mm/s", +1, 0.25, 0.15),
    Feature("vib_de_2x_1x", "Vib DE 2×/1× ratio", "-", +1, 0.15, 0.2, key=False),
    Feature("temp_de_rise", "Bearing DE rise (steady-state est.)", "K", +1, 4.0, 0.15),
    Feature("temp_nde_rise", "Bearing NDE rise (steady-state est.)", "K", +1, 4.0, 0.15),
    Feature("winding_rise", "Winding rise (steady-state est.)", "K", +1, 6.0, 0.10),
    Feature("ir40_log", "log10 IR @40 °C", "log10 MΩ", -1, 0.15, 0.0),
    Feature("current_pct", "Current", "% rated", 0, 2.0, 0.0),
    Feature("current_imbalance", "Current imbalance", "%", +1, 0.8, 0.0),
    Feature("airflow_pct", "Airflow @rated rpm", "% rated", -1, 3.0, 0.0),
    Feature("dp_pct", "ΔP @rated rpm", "% rated", +1, 4.0, 0.0),
    Feature("noise", "Noise", "dB(A)", +1, 1.5, 0.0),
    Feature("runup", "Run-up time", "s", +1, 0.8, 0.08),
    Feature("coastdown", "Coast-down time", "s", -1, 5.0, 0.06),
    Feature("inrush_ratio", "Inrush / rated current", "×", 0, 0.25, 0.06, key=False),
]}
FEATURE_NAMES: tuple[str, ...] = tuple(FEATURES)

# Feature groups: a corrective repair only resets the baselines of groups it physically changes.
GROUP_OF: dict[str, str] = {
    **{n: "vib" for n in FEATURE_NAMES if n.startswith("vib_")},
    "temp_de_rise": "bearing_temp", "temp_nde_rise": "bearing_temp", "winding_rise": "winding",
    "ir40_log": "insulation", "current_pct": "electrical", "current_imbalance": "electrical",
    "airflow_pct": "aero", "dp_pct": "aero", "noise": "acoustic",
    "runup": "start_stop", "coastdown": "start_stop", "inrush_ratio": "start_stop",
}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(GROUP_OF.values()))
# Raw measurement → group (for suspect screening); ambient is never reset.
PARAM_GROUP: dict[str, str | None] = {
    "temp_bearing_de_c": "bearing_temp", "temp_bearing_nde_c": "bearing_temp", "temp_winding_c": "winding",
    "insulation_resistance_mohm": "insulation", "voltage_v": "electrical", "current_imbalance_pct": "electrical",
    "current_l1_a": "electrical", "current_l2_a": "electrical", "current_l3_a": "electrical",
    "rpm": "aero", "airflow_m3s": "aero", "diff_pressure_pa": "aero", "noise_dba": "acoustic",
    "runup_time_s": "start_stop", "coastdown_time_s": "start_stop", "inrush_peak_a": "start_stop",
    "ambient_temp_c": None,
}
# Failure mode repaired → feature groups whose baseline it changes. Unknown repairs reset all.
MODE_RESETS: dict[str, tuple[str, ...]] = {
    "bearing_wear": ("vib", "bearing_temp", "acoustic", "start_stop"),
    "lubrication_loss": ("vib", "bearing_temp", "acoustic", "start_stop"),
    "impeller_imbalance": ("vib", "acoustic", "aero", "electrical"),
    "misalignment_looseness": ("vib", "bearing_temp", "acoustic"),
    "insulation_degradation": ("winding", "insulation", "electrical", "start_stop"),
    "airflow_restriction": ("aero", "electrical", "acoustic"),
    "other": ("start_stop",),
}


def seg_col(group: str | None) -> str:
    return f"seg_{group}" if group else "seg_none"


META = ["fan_id", "ts", "segment", "run_type", "duration_min", "run_hours_cum", "starts_cum",
        *[f"seg_{g}" for g in GROUPS]]


def _rise(t: pd.Series, amb: pd.Series, dur: pd.Series, tau: float) -> pd.Series:
    frac = 1 - np.exp(-dur / tau)
    return ((t - amb) / frac).where(frac >= MIN_THERMAL_FRACTION)


def _broadband(rms: pd.Series, c1: pd.Series, c2: pd.Series) -> pd.Series:
    return np.sqrt((rms**2 - c1**2 - c2**2).clip(lower=0.0))


def compute_features(runs: pd.DataFrame, fans: pd.DataFrame, cfg: FeaturesCfg) -> pd.DataFrame:
    """One row per run (same index as `runs`): META + FEATURE_NAMES + d_<feature> deltas."""
    r = runs
    nameplate = fans.reindex(r["fan_id"])
    rated_i = nameplate["rated_current_a"].to_numpy()
    rated_rpm = nameplate["rated_rpm"].to_numpy()
    rpm = r["rpm"].fillna(pd.Series(rated_rpm, index=r.index))
    speed = rpm / rated_rpm
    amb = r["ambient_temp_c"]
    tau = cfg.thermal_tau_min

    f = pd.DataFrame(index=r.index)
    f["vib_de_rms"] = r["vib_de_rms_mms"]
    f["vib_nde_rms"] = r["vib_nde_rms_mms"]
    f["vib_de_1x"] = r["vib_de_1x_mms"]
    f["vib_nde_1x"] = r["vib_nde_1x_mms"]
    f["vib_de_2x"] = r["vib_de_2x_mms"]
    f["vib_nde_2x"] = r["vib_nde_2x_mms"]
    f["vib_de_bb"] = _broadband(r["vib_de_rms_mms"], r["vib_de_1x_mms"], r["vib_de_2x_mms"])
    f["vib_nde_bb"] = _broadband(r["vib_nde_rms_mms"], r["vib_nde_1x_mms"], r["vib_nde_2x_mms"])
    f["vib_de_2x_1x"] = r["vib_de_2x_mms"] / r["vib_de_1x_mms"].where(r["vib_de_1x_mms"] > 0)
    f["temp_de_rise"] = _rise(r["temp_bearing_de_c"], amb, r["duration_min"], tau)
    f["temp_nde_rise"] = _rise(r["temp_bearing_nde_c"], amb, r["duration_min"], tau)
    f["winding_rise"] = _rise(r["temp_winding_c"], amb, r["duration_min"], tau)
    ir40 = r["insulation_resistance_mohm"] * 0.5 ** ((cfg.ir_reference_c - amb.fillna(30.0)) / 10.0)
    f["ir40_log"] = np.log10(ir40.where(ir40 > 0))
    phases = r[["current_l1_a", "current_l2_a", "current_l3_a"]]
    mean_i = phases.mean(axis=1).where(phases.notna().sum(axis=1) >= 2)
    f["current_pct"] = mean_i / rated_i * 100.0
    dev = phases.sub(phases.mean(axis=1), axis=0).abs().max(axis=1) / phases.mean(axis=1) * 100.0
    f["current_imbalance"] = dev.where(phases.notna().all(axis=1), r["current_imbalance_pct"])
    f["airflow_pct"] = r["airflow_m3s"] / speed / nameplate["rated_airflow_m3s"].to_numpy() * 100.0
    f["dp_pct"] = r["diff_pressure_pa"] / speed**2 / nameplate["rated_diff_pressure_pa"].to_numpy() * 100.0
    f["noise"] = r["noise_dba"]
    f["runup"] = r["runup_time_s"]
    f["coastdown"] = r["coastdown_time_s"]
    f["inrush_ratio"] = r["inrush_peak_a"] / rated_i
    f = f.astype("float64")

    meta = r[["fan_id", "ts", "segment", "run_type", "duration_min", *[f"seg_{g}" for g in GROUPS]]].copy()
    grp = r.groupby("fan_id", sort=False)
    meta["run_hours_cum"] = grp["duration_min"].cumsum() / 60.0
    meta["starts_cum"] = grp.cumcount() + 1

    deltas = {}
    for name in FEATURE_NAMES:
        keys = [r["fan_id"], r[seg_col(GROUP_OF[name])]]
        prev = f[name].groupby(keys, sort=False).transform(lambda s: s.ffill().shift(1))
        deltas[f"d_{name}"] = f[name] - prev
    return pd.concat([meta, f, pd.DataFrame(deltas, index=f.index)], axis=1)
