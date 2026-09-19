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


@pytest.fixture(scope="session")
def sim_fleet(tmp_path_factory):
    """A small simulated fleet loaded the way the engine sees it (never ground truth)."""
    from tbfmon.engine import load_fleet
    from tbfmon.ingest import ingest
    from tbfmon.limits import load_limits
    from tbfmon.pm import data_as_of
    from tbfmon.simulate import generate
    from tbfmon.sources import SimulatorSource

    path = tmp_path_factory.mktemp("fleet") / "t.db"
    c = db.connect(path, create=True)
    db.init_schema(c)
    ingest(c, SimulatorSource(generate(n_fans=20, years=3, seed=11)))
    return load_fleet(c, load_limits(), data_as_of(c))


@pytest.fixture(scope="session")
def engine_cfg():
    from tbfmon.engine import load_engine_config
    return load_engine_config()


@pytest.fixture(scope="session")
def cli_home(tmp_path_factory):
    """An initialised workspace with a simulated fleet, shared by the operator-command tests."""
    from typer.testing import CliRunner

    from tbfmon.cli import app

    home = tmp_path_factory.mktemp("cli_home")
    runner = CliRunner()
    for args in (["init"], ["simulate", "--fans", "16", "--years", "3", "--seed", "4"]):
        res = runner.invoke(app, ["--home", str(home), *args])
        assert res.exit_code == 0, res.output
    return home
