"""Domain models (pydantic v2) and the measurement catalogue. See DESIGN.md §3."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RunType(StrEnum):
    MOCK_DRILL = "mock_drill"
    SCHEDULED_TEST = "scheduled_test"
    COMMISSIONING = "commissioning"


class MaintenanceType(StrEnum):
    PREVENTIVE = "preventive"
    CORRECTIVE = "corrective"


class FailureMode(StrEnum):
    BEARING_WEAR = "bearing_wear"
    IMPELLER_IMBALANCE = "impeller_imbalance"
    MISALIGNMENT_LOOSENESS = "misalignment_looseness"
    INSULATION_DEGRADATION = "insulation_degradation"
    AIRFLOW_RESTRICTION = "airflow_restriction"
    LUBRICATION_LOSS = "lubrication_loss"
    OTHER = "other"


class SymptomDirection(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    ERRATIC = "erratic"


# --------------------------------------------------------------------------
# Measurement catalogue
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Measurement:
    name: str
    unit: str
    label: str
    group: str
    # Sensor range: values outside are physically impossible for the instrument
    # and are nulled at ingest (DESIGN.md §7 decision 2).
    min: float
    max: float


_M = Measurement
MEASUREMENTS: dict[str, Measurement] = {
    m.name: m
    for m in [
        _M("vib_de_rms_mms", "mm/s", "Vib DE RMS", "vibration", 0.0, 50.0),
        _M("vib_de_1x_mms", "mm/s", "Vib DE 1x", "vibration", 0.0, 50.0),
        _M("vib_de_2x_mms", "mm/s", "Vib DE 2x", "vibration", 0.0, 50.0),
        _M("vib_nde_rms_mms", "mm/s", "Vib NDE RMS", "vibration", 0.0, 50.0),
        _M("vib_nde_1x_mms", "mm/s", "Vib NDE 1x", "vibration", 0.0, 50.0),
        _M("vib_nde_2x_mms", "mm/s", "Vib NDE 2x", "vibration", 0.0, 50.0),
        _M("temp_bearing_de_c", "°C", "Bearing DE temp", "thermal", -10.0, 200.0),
        _M("temp_bearing_nde_c", "°C", "Bearing NDE temp", "thermal", -10.0, 200.0),
        _M("temp_winding_c", "°C", "Winding temp", "thermal", -10.0, 250.0),
        _M("current_l1_a", "A", "Current L1", "electrical", 0.0, 2000.0),
        _M("current_l2_a", "A", "Current L2", "electrical", 0.0, 2000.0),
        _M("current_l3_a", "A", "Current L3", "electrical", 0.0, 2000.0),
        _M("current_imbalance_pct", "%", "Current imbalance", "electrical", 0.0, 100.0),
        _M("voltage_v", "V", "Voltage", "electrical", 0.0, 1000.0),
        _M("rpm", "rpm", "Speed", "mechanical", 0.0, 3600.0),
        _M("airflow_m3s", "m³/s", "Airflow", "aero", 0.0, 500.0),
        _M("diff_pressure_pa", "Pa", "Diff. pressure", "aero", 0.0, 5000.0),
        _M("noise_dba", "dB(A)", "Noise", "acoustic", 30.0, 140.0),
        _M("insulation_resistance_mohm", "MΩ", "Insulation resistance", "electrical", 0.0, 100000.0),
        _M("runup_time_s", "s", "Run-up time", "mechanical", 0.0, 120.0),
        _M("coastdown_time_s", "s", "Coast-down time", "mechanical", 0.0, 1200.0),
        _M("inrush_peak_a", "A", "Inrush peak", "electrical", 0.0, 10000.0),
        _M("ambient_temp_c", "°C", "Ambient temp", "thermal", -10.0, 60.0),
    ]
}
MEASUREMENT_FIELDS: tuple[str, ...] = tuple(MEASUREMENTS)


def compute_current_imbalance(l1: float | None, l2: float | None, l3: float | None) -> float | None:
    """NEMA definition: max deviation from the mean, as % of the mean."""
    if l1 is None or l2 is None or l3 is None:
        return None
    mean = (l1 + l2 + l3) / 3.0
    if mean <= 0:
        return None
    return round(max(abs(l1 - mean), abs(l2 - mean), abs(l3 - mean)) / mean * 100.0, 3)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

_STRICT = ConfigDict(extra="forbid", str_strip_whitespace=True, use_enum_values=False)
NonEmptyStr = Annotated[str, Field(min_length=1)]
PositiveFloat = Annotated[float, Field(gt=0)]


class PMTask(BaseModel):
    model_config = _STRICT

    task: NonEmptyStr
    interval_months: Annotated[int, Field(gt=0)] | None = None
    interval_run_hours: PositiveFloat | None = None
    needs_work_at_height: bool

    @model_validator(mode="after")
    def _needs_an_interval(self) -> "PMTask":
        if self.interval_months is None and self.interval_run_hours is None:
            raise ValueError("PM task needs interval_months and/or interval_run_hours")
        return self


class PMTaskRow(PMTask):
    """A PM task as a flat row (CSV / DB), carrying its fan_id."""

    fan_id: NonEmptyStr


class Fan(BaseModel):
    model_config = _STRICT

    fan_id: NonEmptyStr
    station: NonEmptyStr
    line: str | None = None
    tunnel_section: NonEmptyStr
    capacity_class: NonEmptyStr
    rated_power_kw: PositiveFloat
    rated_airflow_m3s: PositiveFloat
    rated_diff_pressure_pa: PositiveFloat
    rated_rpm: PositiveFloat
    rated_voltage_v: PositiveFloat
    rated_current_a: PositiveFloat
    oem_model: NonEmptyStr
    install_date: dt.date
    pm_schedule: list[PMTask] = Field(default_factory=list)

    @field_validator("pm_schedule")
    @classmethod
    def _unique_tasks(cls, v: list[PMTask]) -> list[PMTask]:
        names = [t.task for t in v]
        if len(names) != len(set(names)):
            raise ValueError("duplicate PM task names")
        return v


def _measure(name: str):
    m = MEASUREMENTS[name]
    return Field(default=None, ge=m.min, le=m.max, description=f"{m.label} [{m.unit}]")


class TestRun(BaseModel):
    model_config = _STRICT
    __test__ = False  # not a pytest test class

    fan_id: NonEmptyStr
    timestamp: dt.datetime
    run_type: RunType
    duration_min: PositiveFloat

    vib_de_rms_mms: float | None = _measure("vib_de_rms_mms")
    vib_de_1x_mms: float | None = _measure("vib_de_1x_mms")
    vib_de_2x_mms: float | None = _measure("vib_de_2x_mms")
    vib_nde_rms_mms: float | None = _measure("vib_nde_rms_mms")
    vib_nde_1x_mms: float | None = _measure("vib_nde_1x_mms")
    vib_nde_2x_mms: float | None = _measure("vib_nde_2x_mms")
    temp_bearing_de_c: float | None = _measure("temp_bearing_de_c")
    temp_bearing_nde_c: float | None = _measure("temp_bearing_nde_c")
    temp_winding_c: float | None = _measure("temp_winding_c")
    current_l1_a: float | None = _measure("current_l1_a")
    current_l2_a: float | None = _measure("current_l2_a")
    current_l3_a: float | None = _measure("current_l3_a")
    current_imbalance_pct: float | None = _measure("current_imbalance_pct")
    voltage_v: float | None = _measure("voltage_v")
    rpm: float | None = _measure("rpm")
    airflow_m3s: float | None = _measure("airflow_m3s")
    diff_pressure_pa: float | None = _measure("diff_pressure_pa")
    noise_dba: float | None = _measure("noise_dba")
    insulation_resistance_mohm: float | None = _measure("insulation_resistance_mohm")
    runup_time_s: float | None = _measure("runup_time_s")
    coastdown_time_s: float | None = _measure("coastdown_time_s")
    inrush_peak_a: float | None = _measure("inrush_peak_a")
    ambient_temp_c: float | None = _measure("ambient_temp_c")

    @model_validator(mode="after")
    def _fill_imbalance(self) -> "TestRun":
        if self.current_imbalance_pct is None:
            self.current_imbalance_pct = compute_current_imbalance(
                self.current_l1_a, self.current_l2_a, self.current_l3_a
            )
        return self

    def measurements(self) -> dict[str, float | None]:
        return {k: getattr(self, k) for k in MEASUREMENT_FIELDS}


class MaintenanceRecord(BaseModel):
    model_config = _STRICT

    fan_id: NonEmptyStr
    date: dt.date
    task: NonEmptyStr
    type: MaintenanceType
    parts_replaced: list[str] = Field(default_factory=list)
    findings: str = ""
    man_hours: Annotated[float, Field(ge=0)] | None = None
    work_at_height: bool | None = None

    @field_validator("parts_replaced", mode="before")
    @classmethod
    def _split_parts(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.split(";") if p.strip()]
        return v


class Symptom(BaseModel):
    model_config = _STRICT

    parameter: NonEmptyStr
    direction: SymptomDirection
    note: str = ""

    @field_validator("parameter")
    @classmethod
    def _known_parameter(cls, v: str) -> str:
        if v not in MEASUREMENTS:
            raise ValueError(f"unknown measurement parameter {v!r}")
        return v


def parse_symptoms(text: str | None) -> list[dict]:
    """Parse CSV encoding `param:direction:note|param:direction:note`."""
    if not text:
        return []
    out = []
    for item in str(text).split("|"):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":", 2)
        if len(parts) < 2:
            raise ValueError(f"bad symptom {item!r}; expected param:direction[:note]")
        out.append({"parameter": parts[0].strip(), "direction": parts[1].strip(),
                    "note": parts[2].strip() if len(parts) > 2 else ""})
    return out


def format_symptoms(symptoms: list[Symptom]) -> str:
    return "|".join(f"{s.parameter}:{s.direction.value}:{s.note}" for s in symptoms)


class FailureRecord(BaseModel):
    model_config = _STRICT

    fan_id: NonEmptyStr
    date: dt.date
    failure_mode: FailureMode
    symptoms: list[Symptom] = Field(default_factory=list)
    root_cause: str = ""
    fix: str = ""
    downtime_hours: Annotated[float, Field(ge=0)] | None = None

    @field_validator("symptoms", mode="before")
    @classmethod
    def _parse_symptoms(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return parse_symptoms(v)
        return v
