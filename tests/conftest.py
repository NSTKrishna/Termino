from __future__ import annotations

import datetime as dt

import pytest

from tbfmon import db
from tbfmon.models import Fan, PMTask


@pytest.fixture
def fan_data() -> dict:
    return {
        "fan_id": "TBF-001",
        "station": "Rajiv Chowk",
        "line": "Yellow",
        "tunnel_section": "UP Rajiv Chowk–Patel Chowk",
        "capacity_class": "C45",
        "rated_power_kw": 45.0,
        "rated_airflow_m3s": 35.0,
        "rated_diff_pressure_pa": 600.0,
        "rated_rpm": 1480.0,
        "rated_voltage_v": 415.0,
        "rated_current_a": 80.0,
        "oem_model": "AeroTun JF-45/4R",
        "install_date": "2012-05-01",
        "pm_schedule": [
            {"task": "Bearing regreasing", "interval_months": 6, "needs_work_at_height": True},
            {"task": "Insulation resistance test", "interval_months": 12, "needs_work_at_height": False},
        ],
    }


@pytest.fixture
def fan(fan_data) -> Fan:
    return Fan.model_validate(fan_data)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db", create=True)
    db.init_schema(c)
    yield c
    c.close()
