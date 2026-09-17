"""Schema validation for the pydantic domain models."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from tbfmon.models import (
    MEASUREMENTS,
    FailureMode,
    FailureRecord,
    Fan,
    MaintenanceRecord,
    PMTask,
    RunType,
    TestRun,
    compute_current_imbalance,
    format_symptoms,
)


def _run(**kw):
    base = {"fan_id": "TBF-001", "timestamp": "2024-03-02T01:30:00", "run_type": "mock_drill", "duration_min": 30}
    return TestRun.model_validate({**base, **kw})


def test_fan_valid(fan):
    assert fan.install_date == dt.date(2012, 5, 1)
    assert fan.pm_schedule[0].needs_work_at_height is True


@pytest.mark.parametrize("field,value", [
    ("rated_power_kw", 0),
    ("rated_current_a", -5),
    ("fan_id", ""),
    ("install_date", "not-a-date"),
])
def test_fan_rejects_bad_values(fan_data, field, value):
    with pytest.raises(ValidationError):
        Fan.model_validate({**fan_data, field: value})


def test_fan_rejects_unknown_fields(fan_data):
    with pytest.raises(ValidationError):
        Fan.model_validate({**fan_data, "colour": "blue"})


def test_fan_rejects_duplicate_pm_tasks(fan_data):
    t = {"task": "X", "interval_months": 3, "needs_work_at_height": True}
    with pytest.raises(ValidationError):
        Fan.model_validate({**fan_data, "pm_schedule": [t, t]})


def test_pm_task_needs_an_interval():
    with pytest.raises(ValidationError):
        PMTask(task="Inspect", needs_work_at_height=False)
    assert PMTask(task="Inspect", interval_run_hours=500, needs_work_at_height=False).interval_months is None


def test_test_run_all_measurements_nullable():
    r = _run()
    assert r.run_type is RunType.MOCK_DRILL
    assert all(v is None for v in r.measurements().values())


def test_test_run_run_type_enum():
    with pytest.raises(ValidationError):
        _run(run_type="joyride")


def test_test_run_duration_positive():
    with pytest.raises(ValidationError):
        _run(duration_min=0)


@pytest.mark.parametrize("name", list(MEASUREMENTS))
def test_test_run_rejects_out_of_sensor_range(name):
    m = MEASUREMENTS[name]
    with pytest.raises(ValidationError):
        _run(**{name: m.max + 1})
    with pytest.raises(ValidationError):
        _run(**{name: m.min - 1})


def test_current_imbalance_computed():
    r = _run(current_l1_a=80, current_l2_a=82, current_l3_a=78)
    assert r.current_imbalance_pct == pytest.approx(2.5)
    assert compute_current_imbalance(80, None, 78) is None


def test_current_imbalance_kept_when_given():
    assert _run(current_l1_a=80, current_l2_a=82, current_l3_a=78, current_imbalance_pct=9.0).current_imbalance_pct == 9.0


def test_maintenance_parts_from_csv_string():
    m = MaintenanceRecord.model_validate({
        "fan_id": "TBF-001", "date": "2024-01-10", "task": "Replace DE bearing",
        "type": "corrective", "parts_replaced": "bearing 6314 C3; grease",
    })
    assert m.parts_replaced == ["bearing 6314 C3", "grease"]
    with pytest.raises(ValidationError):
        MaintenanceRecord.model_validate({**m.model_dump(), "type": "sometimes"})


def test_failure_symptoms_roundtrip():
    f = FailureRecord.model_validate({
        "fan_id": "TBF-001", "date": "2024-05-01", "failure_mode": "bearing_wear",
        "symptoms": "vib_de_rms_mms:increase:1.2→6.3 mm/s|temp_bearing_de_c:increase:",
    })
    assert f.failure_mode is FailureMode.BEARING_WEAR
    assert [s.parameter for s in f.symptoms] == ["vib_de_rms_mms", "temp_bearing_de_c"]
    again = FailureRecord.model_validate({**f.model_dump(), "symptoms": format_symptoms(f.symptoms)})
    assert again == f


def test_failure_symptom_parameter_must_be_known():
    with pytest.raises(ValidationError):
        FailureRecord.model_validate({
            "fan_id": "TBF-001", "date": "2024-05-01", "failure_mode": "other",
            "symptoms": "smell:increase:burning",
        })
