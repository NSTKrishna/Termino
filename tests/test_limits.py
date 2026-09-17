"""limits.yaml schema, placeholder marking and evaluation."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from tbfmon.limits import LimitsFile, Status, default_limits_text, evaluate, load_limits


def test_default_limits_load_and_cover_classes():
    lf = load_limits()
    assert set(lf.classes) == {"C30", "C45", "C55"}
    assert lf.for_class("C30")["noise_dba"].warn_high == 92


def test_every_default_limit_is_marked_placeholder():
    lf = load_limits()
    total = len(lf.defaults) + sum(len(c.overrides) for c in lf.classes.values())
    assert lf.placeholder_count() == total
    text = default_limits_text()
    assert "REPLACE WITH OEM VALUES" in text
    assert text.count("REPLACE WITH OEM VALUES") >= total


def test_limits_reject_unknown_parameter():
    data = yaml.safe_load(default_limits_text())
    data["defaults"]["flux_capacitor"] = {"warn_high": 1}
    with pytest.raises(ValidationError):
        LimitsFile.model_validate(data)


def test_limits_reject_inverted_thresholds():
    data = yaml.safe_load(default_limits_text())
    data["defaults"]["vib_de_rms_mms"]["warn_high"] = 9.0
    with pytest.raises(ValidationError):
        LimitsFile.model_validate(data)


def test_evaluate_bases():
    lf = load_limits().for_class("C45")
    fan = {"rated_current_a": 80.0, "rated_airflow_m3s": 35.0}
    assert evaluate("vib_de_rms_mms", 2.0, lf["vib_de_rms_mms"], fan)[0] is Status.OK
    assert evaluate("vib_de_rms_mms", 5.0, lf["vib_de_rms_mms"], fan)[0] is Status.WARN
    assert evaluate("vib_de_rms_mms", 8.0, lf["vib_de_rms_mms"], fan)[0] is Status.ALARM
    assert evaluate("current_l1_a", 90.0, lf["current_l1_a"], fan) == (Status.WARN, pytest.approx(112.5))
    assert evaluate("airflow_m3s", 30.0, lf["airflow_m3s"], fan)[0] is Status.WARN
    assert evaluate("temp_bearing_de_c", 80.0, lf["temp_bearing_de_c"], fan, ambient=30.0)[0] is Status.WARN
    assert evaluate("temp_bearing_de_c", 80.0, lf["temp_bearing_de_c"], fan, ambient=None)[0] is Status.NA
    assert evaluate("insulation_resistance_mohm", 5.0, lf["insulation_resistance_mohm"], fan)[0] is Status.ALARM
    assert evaluate("vib_de_rms_mms", None, lf["vib_de_rms_mms"], fan)[0] is Status.NA
