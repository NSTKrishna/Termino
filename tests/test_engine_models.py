"""Engine layers 4–8 and the pipeline: anomaly, fault modes, trend, health index, recommender."""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from tbfmon.engine import assess, explain_run, recommend_all
from tbfmon.engine.anomaly import detect
from tbfmon.engine.config import AnomalyCfg, SupervisedCfg, TrendCfg
from tbfmon.engine.features import FEATURE_NAMES
from tbfmon.engine.folds import fan_folds
from tbfmon.engine.health import health, trend_component
from tbfmon.engine.labels import failure_labels, healthy_mask
from tbfmon.engine.recommend import decide_action, recommend_fan, totals
from tbfmon.engine.rules import score_run
from tbfmon.engine.trend import fit
from tbfmon.limits import load_limits


@pytest.fixture(scope="module")
def full(sim_fleet, engine_cfg):
    return assess(sim_fleet, engine_cfg, "full")


# ---- folds (leakage guard) ---------------------------------------------------------------------

def test_fan_folds_never_share_a_fan():
    groups = np.repeat(np.array([f"F{i}" for i in range(9)]), 7)
    folds = list(fan_folds(groups, 5))
    assert len(folds) == 5
    for tr, te in folds:
        assert not set(groups[tr]) & set(groups[te])
    assert sorted(np.concatenate([te for _, te in folds])) == list(range(len(groups)))


# ---- layer 4: anomaly ------------------------------------------------------------------------

def _z_table(n_fans=6, per_fan=30, seed=0):
    rng = np.random.default_rng(seed)
    n = n_fans * per_fan
    feats = pd.DataFrame({"fan_id": np.repeat([f"F{i}" for i in range(n_fans)], per_fan)})
    z = pd.DataFrame({f"z_{f}": rng.normal(0, 1, n) for f in FEATURE_NAMES})
    z["baseline_established"] = True
    fans = pd.DataFrame({"capacity_class": "C45"}, index=[f"F{i}" for i in range(n_fans)])
    return feats, z, fans


@pytest.mark.parametrize("mode", ["full", "oof"])
def test_isolation_forest_flags_multivariate_outlier_and_names_contributors(mode):
    # IF targets unusual *combinations*; single-feature extremes are the drift/limit layers' job.
    feats, z, fans = _z_table()
    for f in ("vib_nde_1x", "vib_nde_rms", "noise"):
        z.loc[5, f"z_{f}"] = 8.0
    res = detect(feats, z, fans, pd.Series(True, index=feats.index), AnomalyCfg(), seed=1, mode=mode)
    assert res.percentile[5] >= 0.98
    assert {c[0] for c in res.contributors[5]} <= {"vib_nde_1x", "vib_nde_rms", "noise"}
    assert res.percentile.drop(5).median() < 0.8


def test_anomaly_deterministic_with_seed():
    feats, z, fans = _z_table()
    a = detect(feats, z, fans, pd.Series(True, index=feats.index), AnomalyCfg(), seed=3)
    b = detect(feats, z, fans, pd.Series(True, index=feats.index), AnomalyCfg(), seed=3)
    pd.testing.assert_series_equal(a.percentile, b.percentile)


# ---- labels (FailureRecord / MaintenanceRecord only) -------------------------------------------

def test_labels_from_records(engine_cfg):
    feats = pd.DataFrame({"fan_id": "F1", "ts": pd.to_datetime(["2023-01-01", "2023-11-01", "2024-03-01",
                                                                "2024-05-30", "2024-06-01"])})
    fails = pd.DataFrame({"fan_id": ["F1"], "date": pd.to_datetime(["2024-06-01"]), "failure_mode": ["bearing_wear"]})
    lab = failure_labels(feats, fails, SupervisedCfg())
    assert lab.tolist() == ["healthy", None, "bearing_wear", "bearing_wear", None]  # failure run itself excluded
    maint = pd.DataFrame({"fan_id": ["F1"], "date": pd.to_datetime(["2023-01-20"]), "type": ["preventive"],
                          "findings": ["Mounting bolts below torque; re-torqued"], "task": ["Torque check"]})
    ok = healthy_mask(feats, fails, maint, engine_cfg.labels)
    assert ok.tolist() == [False, True, False, False, False]


# ---- layer 5a: rules -------------------------------------------------------------------------

def _row(**kv) -> pd.Series:
    return pd.Series({"ts": pd.Timestamp("2025-01-01"), **kv})


def test_rules_imbalance_with_reason():
    r = score_run(_row(vib_de_1x=4.2, vib_de_rms=4.5, b_vib_de_1x=0.9, z_vib_de_1x=7.0, zb_vib_de_1x=7.0))
    assert r[0].mode == "impeller_imbalance" and r[0].confidence == pytest.approx(1.0)
    assert "baseline" in r[0].reasons[0] and "vibration energy" in r[1 - 1].reasons[1]


def test_rules_bearing_wear_location_and_insulation():
    r = score_run(_row(vib_nde_bb=3.0, b_vib_nde_bb=0.7, z_vib_nde_bb=6.5, zb_vib_nde_bb=6.5))
    assert (r[0].mode, r[0].location) == ("bearing_wear", "NDE")
    r = score_run(_row(ir40_log=1.0, b_ir40_log=3.0, z_ir40_log=-8.0, zb_ir40_log=8.0,
                       current_imbalance=4.0, zb_current_imbalance=5.0, z_current_imbalance=5.0))
    assert r[0].mode == "insulation_degradation" and "MΩ" in r[0].reasons[0]


def test_rules_use_current_imbalance_when_ir_unmeasured():
    r = score_run(_row(current_imbalance=5.0, zb_current_imbalance=6.0, z_current_imbalance=6.0))
    assert r[0].mode == "insulation_degradation" and "measure IR" in r[0].reasons[0]


def test_rules_silent_when_normal():
    assert score_run(_row(vib_de_rms=1.2, zb_vib_de_rms=0.3, z_vib_de_rms=0.3)) == []


# ---- layer 6: trend --------------------------------------------------------------------------

def test_trend_insufficient_data():
    r = fit("vib_de_rms", np.array([0.0, 30, 60]), np.array([1.0, 1.1, 1.2]), 7.1, "days", TrendCfg(), 60)
    assert r.status == "insufficient data"


def test_trend_eta_and_ci_for_linear_degradation():
    x = np.arange(0, 300, 30.0)
    y = 1.0 + 0.01 * x + np.random.default_rng(0).normal(0, 0.02, len(x))
    r = fit("vib_de_rms", x, y, 7.1, "days", TrendCfg(), 60)
    assert r.status == "degrading"
    true_eta = (7.1 - (1.0 + 0.01 * x[-1])) / 0.01
    assert r.eta_ci[0] <= true_eta <= r.eta_ci[1]
    assert abs(r.eta - true_eta) / true_eta < 0.2


def test_trend_low_is_bad_and_flat_is_not_significant():
    x = np.arange(0, 300, 30.0)
    r = fit("ir40_log", x, 3.0 - 0.004 * x, 1.0, "days", TrendCfg(), 60)  # log10 IR falling toward 10 MΩ
    assert r.status == "degrading" and r.eta == pytest.approx((3.0 - 0.004 * 270 - 1.0) / 0.004)
    flat = fit("vib_de_rms", x, 1.0 + np.random.default_rng(1).normal(0, 0.05, len(x)), 7.1, "days", TrendCfg(), 60)
    assert flat.status == "no significant trend"


# ---- layer 7: health index -------------------------------------------------------------------

def test_health_index_components_and_overrides(engine_cfg):
    hc = engine_cfg.health_index
    clean = health(pd.Series({"baseline_established": True}), "OK", 0.5, None, None, hc)
    assert (clean.index, clean.status) == (100.0, "Healthy")
    warn = health(pd.Series({"baseline_established": True}), "WARN", 0.5, None, None, hc)
    assert warn.index == 85.0 and warn.status == "Watch"  # WARN caps at Watch
    alarm = health(pd.Series({"baseline_established": True}), "ALARM", 0.5, None, None, hc)
    assert alarm.status == "Critical" and "LIMIT_ALARM" in alarm.reasons
    bad = health(pd.Series({"baseline_established": True, "zb_vib_de_rms": 6.0, "z_vib_de_rms": 6.0,
                            "drift_vib_de_rms": True}), "OK", 1.0, ("bearing_wear", 1.0), ("vib_de_rms", 60.0), hc)
    assert bad.index == pytest.approx(100 - 20 - 15 - 15 - 15)  # = 35
    assert bad.status == "Critical"
    assert trend_component(100) == 0.6 and trend_component(None) == 0.0


# ---- layer 8: recommender --------------------------------------------------------------------

@pytest.mark.parametrize("status,mode,established,stale,expected", [
    ("Critical", None, True, False, "urgent corrective"),
    ("Degraded", None, True, False, "targeted inspection"),
    ("Watch", ("bearing_wear", 0.7), True, False, "targeted inspection"),
    ("Watch", ("bearing_wear", 0.3), True, False, "no action"),
    ("Healthy", None, True, False, "defer OEM PM"),
    ("Healthy", None, False, False, "no action"),
    ("Healthy", None, True, True, "no action"),
])
def test_decide_action(status, mode, established, stale, expected):
    assert decide_action(status, mode, established, stale, 0.5) == expected


def _pm():
    tasks = pd.DataFrame([
        {"fan_id": "F1", "task": "Bearing regreasing", "interval_months": 6, "interval_run_hours": 2000.0, "needs_work_at_height": True},
        {"fan_id": "F1", "task": "Insulation resistance test", "interval_months": 6, "interval_run_hours": None, "needs_work_at_height": False},
    ])
    maint = pd.DataFrame([
        {"fan_id": "F1", "task": "Bearing regreasing", "type": "preventive", "date": pd.Timestamp("2025-10-01")},
        {"fan_id": "F1", "task": "Insulation resistance test", "type": "preventive", "date": pd.Timestamp("2025-10-01")},
    ])
    return tasks, maint


def test_healthy_fan_defers_only_deferrable_tasks(engine_cfg):
    tasks, maint = _pm()
    r = recommend_fan("F1", "Healthy", 95.0, [], None, True, dt.date(2025, 12, 20), tasks, maint,
                      dt.date(2025, 12, 31), engine_cfg.recommender, 0.5, [])
    assert r.action == "defer OEM PM"
    assert [d["task"] for d in r.deferred] == ["Bearing regreasing"]  # IR test never deferred
    assert r.man_hours_cbm < r.man_hours_oem and r.height_hours_cbm < r.height_hours_oem


def test_no_deferral_when_related_mode_suspected_or_data_stale(engine_cfg):
    tasks, maint = _pm()
    r = recommend_fan("F1", "Healthy", 90.0, [("lubrication_loss", 0.6, "DE")], None, True, dt.date(2025, 12, 20),
                      tasks, maint, dt.date(2025, 12, 31), engine_cfg.recommender, 0.5, [])
    assert r.deferred == []
    r = recommend_fan("F1", "Healthy", 95.0, [], None, True, dt.date(2025, 3, 1), tasks, maint,
                      dt.date(2025, 12, 31), engine_cfg.recommender, 0.5, [])
    assert r.action == "no action" and any(c.startswith("STALE_DATA") for c in r.reason_codes)


def test_urgent_corrective_tasks_and_cost(engine_cfg):
    tasks, maint = _pm()
    r = recommend_fan("F1", "Critical", 30.0, [("bearing_wear", 0.9, "NDE")], None, True, dt.date(2025, 12, 20),
                      tasks, maint, dt.date(2025, 12, 31), engine_cfg.recommender, 0.5, ["LIMIT_ALARM"])
    assert r.action == "urgent corrective" and r.tasks == ["Replace NDE bearing"]
    assert r.by_date == "2026-01-07" and r.needs_work_at_height
    assert r.man_hours_cbm > r.man_hours_oem


# ---- pipeline ---------------------------------------------------------------------------------

def test_pipeline_assesses_every_fan_and_explains(full, sim_fleet):
    assert set(full.table["status"]) <= {"Healthy", "Watch", "Degraded", "Critical"}
    assert full.table["health_index"].between(0, 100).all()
    recs = recommend_all(full)
    assert len(recs) == len(sim_fleet.fans)
    t = totals(recs)
    assert t["oem_tasks_in_horizon"] >= t["cbm_tasks_in_horizon"] - t["actions"]["urgent corrective"]
    i = full.latest_index(sim_fleet.fans.index[0])
    e = explain_run(full, i)
    json.dumps(e, default=str)  # serialisable for --json
    assert {"health", "limits", "drift", "anomaly", "fault_modes", "trends"} <= set(e)


def test_pipeline_deterministic(sim_fleet, engine_cfg, full):
    again = assess(sim_fleet, engine_cfg, "full")
    pd.testing.assert_series_equal(full.table["health_index"], again.table["health_index"])


def test_oof_mode_reports_grouped_validation(sim_fleet, engine_cfg):
    a = assess(sim_fleet, engine_cfg, "oof")
    assert a.info["supervised"]["validation"] == "GroupKFold by fan"
    assert a.info["supervised"]["warnings"]  # a small fleet cannot support every mode
