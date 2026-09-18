"""Evaluator scoring logic and the engine CLI commands (assess, recommend, evaluate)."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from typer.testing import CliRunner

from tbfmon.cli import app
from tbfmon.evaluation.evaluate import LABEL, mode_metrics, score_alerts

runner = CliRunner()


def _gt():
    return pd.DataFrame([
        {"fan_id": "F1", "fault_id": "a", "fault_mode": "bearing_wear", "location": "DE",
         "onset_date": pd.Timestamp("2024-01-01"), "failure_date": pd.Timestamp("2024-06-01"),
         "end_date": pd.Timestamp("2024-06-05"), "outcome": "failed"},
        {"fan_id": "F2", "fault_id": "b", "fault_mode": "airflow_restriction", "location": None,
         "onset_date": pd.Timestamp("2024-01-01"), "failure_date": pd.Timestamp("2024-05-01"),
         "end_date": pd.Timestamp("2024-05-03"), "outcome": "failed"},
    ])


def _alerts(rows):
    return pd.DataFrame(rows, columns=["fan_id", "ts", "alert", "mode"]).assign(ts=lambda d: pd.to_datetime(d["ts"]))


def test_detection_lead_time_and_false_alarms():
    alerts = _alerts([
        ("F1", "2023-06-01", True, None),            # before onset − grace: false alarm event
        ("F1", "2023-07-01", True, None),            # same event (consecutive alerts)
        ("F1", "2023-09-01", False, None),
        ("F1", "2024-03-02", True, "bearing_wear"),  # detection, 91 days lead
        ("F1", "2024-06-01", True, "bearing_wear"),  # failure run itself: not a detection
        ("F2", "2024-04-30", False, None),
        ("F2", "2024-05-01", True, None),            # only on the failure date → missed
    ])
    r = score_alerts(alerts, _gt(), fan_years=2.0, data_end=pd.Timestamp("2024-12-31"), grace_days=30)
    assert (r["failures"], r["failures_detected"]) == (2, 1)
    assert r["lead_days"]["median"] == 91
    assert r["alert_events"] == 3 and r["false_alarm_events"] == 1
    assert r["false_alarms_per_fan_year"] == 0.5
    assert r["failure_recall_by_mode"] == {"airflow_restriction": "0/1", "bearing_wear": "1/1"}


def test_mode_metrics_ignores_unnamed():
    m = mode_metrics([("bearing_wear", None, "bearing_wear"), ("impeller_imbalance", "bearing_wear", None)], "last")
    assert m["bearing_wear"]["precision"] == 1.0 and m["_overall"]["mode_named"] == 1
    m = mode_metrics([("bearing_wear", None, "bearing_wear"), ("impeller_imbalance", "bearing_wear", None)], "first")
    assert m["bearing_wear"]["precision"] == 0.0 and m["impeller_imbalance"]["recall"] == 0.0


@pytest.fixture(scope="module")
def home(tmp_path_factory):
    h = tmp_path_factory.mktemp("home")
    for args in (["init"], ["simulate", "--fans", "16", "--years", "3", "--seed", "4"]):
        res = runner.invoke(app, ["--home", str(h), *args])
        assert res.exit_code == 0, res.output
    return h


def _json(home, *args):
    res = runner.invoke(app, ["--home", str(home), *args, "--json"])
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def test_init_writes_engine_config(home):
    assert (home / "engine.yaml").exists()


def test_assess_fleet_and_fan_json(home):
    fleet = _json(home, "assess")
    assert len(fleet["fans"]) == 16
    worst = fleet["fans"][0]
    assert {"health_index", "status", "reasons", "action", "components"} <= set(worst)
    detail = _json(home, "assess", "--fan", worst["fan_id"])
    assert detail["recommendation"]["fan_id"] == worst["fan_id"]
    assert {t["axis"] for t in detail["trends_all_axes"]} == {"days", "run_hours_cum", "starts_cum"}


def test_recommend_json_has_savings_and_assumptions(home):
    r = _json(home, "recommend")
    assert r["totals"]["man_hours_oem"] >= 0 and "man_hours_saved" in r["totals"]
    assert r["assumptions"]["note"] == "prevented-failure benefits not counted"
    assert {x["action"] for x in r["recommendations"]} <= {"no action", "defer OEM PM", "targeted inspection",
                                                           "urgent corrective"}


def test_evaluate_labelled_simulated(home):
    r = _json(home, "evaluate")
    assert r["label"] == LABEL and "SIMULATED DATA" in r["label"]
    assert {"CBM engine (inspect/urgent)", "Threshold-only (WARN or ALARM)"} <= set(r["systems"])
    assert "recall" in r["oem_baseline"]
    human = runner.invoke(app, ["--home", str(home), "evaluate"])
    assert human.exit_code == 0 and human.output.count("SIMULATED DATA") >= 3


def test_evaluate_without_ground_truth_fails_cleanly(tmp_path):
    runner.invoke(app, ["--home", str(tmp_path), "init"])
    res = runner.invoke(app, ["--home", str(tmp_path), "evaluate", "--json"])
    assert res.exit_code == 2 and "not found" in json.loads(res.stdout)["error"]
