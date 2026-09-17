"""Permissible limits: schema, loading and evaluation. See DESIGN.md §3 (limits.yaml)."""

from __future__ import annotations

from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import MEASUREMENTS

# Nameplate field used by `pct_of_rated` for each parameter.
RATED_REF: dict[str, str] = {
    "current_l1_a": "rated_current_a",
    "current_l2_a": "rated_current_a",
    "current_l3_a": "rated_current_a",
    "inrush_peak_a": "rated_current_a",
    "voltage_v": "rated_voltage_v",
    "rpm": "rated_rpm",
    "airflow_m3s": "rated_airflow_m3s",
    "diff_pressure_pa": "rated_diff_pressure_pa",
}


class Status(StrEnum):
    OK = "OK"
    WARN = "WARN"
    ALARM = "ALARM"
    NA = "NA"

    @property
    def rank(self) -> int:
        return {"NA": 0, "OK": 1, "WARN": 2, "ALARM": 3}[self.value]


def worst(statuses) -> Status:
    return max(statuses, key=lambda s: s.rank, default=Status.NA)


class Limit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    basis: Literal["absolute", "pct_of_rated", "rise_over_ambient"] = "absolute"
    unit: str = ""
    warn_high: float | None = None
    alarm_high: float | None = None
    warn_low: float | None = None
    alarm_low: float | None = None
    placeholder: bool = True
    note: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> "Limit":
        if all(v is None for v in (self.warn_high, self.alarm_high, self.warn_low, self.alarm_low)):
            raise ValueError("limit needs at least one of warn/alarm high/low")
        if self.warn_high is not None and self.alarm_high is not None and self.warn_high > self.alarm_high:
            raise ValueError("warn_high must be <= alarm_high")
        if self.warn_low is not None and self.alarm_low is not None and self.warn_low < self.alarm_low:
            raise ValueError("warn_low must be >= alarm_low")
        return self


class CapacityClass(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    overrides: dict[str, Limit] = Field(default_factory=dict)


class LimitsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    status: str = "PLACEHOLDER"
    defaults: dict[str, Limit]
    classes: dict[str, CapacityClass]

    @model_validator(mode="after")
    def _known_parameters(self) -> "LimitsFile":
        names = set(self.defaults)
        for c in self.classes.values():
            names |= set(c.overrides)
        unknown = sorted(n for n in names if n not in MEASUREMENTS)
        if unknown:
            raise ValueError(f"unknown parameters in limits: {unknown}")
        for name, lim in self.defaults.items():
            if lim.basis == "pct_of_rated" and name not in RATED_REF:
                raise ValueError(f"{name}: pct_of_rated not supported (no nameplate reference)")
        return self

    def for_class(self, capacity_class: str) -> dict[str, Limit]:
        if capacity_class not in self.classes:
            raise KeyError(f"capacity class {capacity_class!r} not in limits.yaml")
        return {**self.defaults, **self.classes[capacity_class].overrides}

    def placeholder_count(self) -> int:
        n = sum(1 for lim in self.defaults.values() if lim.placeholder)
        for c in self.classes.values():
            n += sum(1 for lim in c.overrides.values() if lim.placeholder)
        return n


def default_limits_text() -> str:
    return resources.files("tbfmon").joinpath("data/limits.yaml").read_text(encoding="utf-8")


def load_limits(path: Path | None = None) -> LimitsFile:
    text = path.read_text(encoding="utf-8") if path and path.exists() else default_limits_text()
    return LimitsFile.model_validate(yaml.safe_load(text))


def evaluate(
    param: str,
    value: float | None,
    limit: Limit | None,
    fan: dict[str, Any],
    ambient: float | None = None,
) -> tuple[Status, float | None]:
    """Return (status, compared_value). compared_value is in the limit's basis."""
    if value is None or limit is None:
        return Status.NA, None
    if limit.basis == "pct_of_rated":
        rated = fan.get(RATED_REF[param])
        if not rated:
            return Status.NA, None
        x = value / rated * 100.0
    elif limit.basis == "rise_over_ambient":
        if ambient is None:
            return Status.NA, None
        x = value - ambient
    else:
        x = value
    if (limit.alarm_high is not None and x >= limit.alarm_high) or (
        limit.alarm_low is not None and x <= limit.alarm_low
    ):
        return Status.ALARM, x
    if (limit.warn_high is not None and x >= limit.warn_high) or (
        limit.warn_low is not None and x <= limit.warn_low
    ):
        return Status.WARN, x
    return Status.OK, x


def evaluate_run(run: dict[str, Any], limits: dict[str, Limit], fan: dict[str, Any]) -> dict[str, tuple[Status, float | None]]:
    ambient = run.get("ambient_temp_c")
    return {p: evaluate(p, run.get(p), lim, fan, ambient) for p, lim in limits.items()}


def describe(limit: Limit) -> str:
    """Short human text like '≥4.5 / ≥7.1' or '≤100 / ≤10 MΩ'."""
    parts = []
    if limit.warn_high is not None or limit.alarm_high is not None:
        parts.append(f"≥{_fmt(limit.warn_high)}/{_fmt(limit.alarm_high)}")
    if limit.warn_low is not None or limit.alarm_low is not None:
        parts.append(f"≤{_fmt(limit.warn_low)}/{_fmt(limit.alarm_low)}")
    suffix = {"pct_of_rated": " %rated", "rise_over_ambient": " K rise"}.get(limit.basis, "")
    return " ".join(parts) + suffix


def _fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:g}"
