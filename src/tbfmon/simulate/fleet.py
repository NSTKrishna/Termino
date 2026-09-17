"""Synthetic fleet: stations, capacity classes, OEM models, PM schedules, baselines.

Station names are real underground DMRC corridors used for illustration only;
OEM names and models are fictional. See DESIGN.md §4.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np

# Underground corridors (illustrative). Sections run station[i] -> station[i+1].
CORRIDORS: list[tuple[str, list[str]]] = [
    ("Yellow", [
        "Vishwavidyalaya", "Vidhan Sabha", "Civil Lines", "Kashmere Gate", "Chandni Chowk",
        "Chawri Bazar", "New Delhi", "Rajiv Chowk", "Patel Chowk", "Central Secretariat",
        "Udyog Bhawan", "Lok Kalyan Marg", "Jor Bagh", "INA", "AIIMS", "Green Park",
        "Hauz Khas", "Malviya Nagar", "Saket",
    ]),
    ("Violet", [
        "Central Secretariat", "Janpath", "Mandi House", "ITO", "Delhi Gate",
        "Jama Masjid", "Lal Qila", "Kashmere Gate",
    ]),
    ("Blue", [
        "Rajiv Chowk", "Barakhamba Road", "Mandi House", "Supreme Court",
    ]),
]
FANS_PER_SECTION = 4  # UP-1, UP-2, DN-1, DN-2 at the station end of each section


@dataclass(frozen=True)
class ClassSpec:
    power_kw: float
    airflow_m3s: float
    diff_pressure_pa: float
    current_a: float
    noise_dba: float
    coastdown_s: float


CLASSES: dict[str, ClassSpec] = {
    "C30": ClassSpec(30.0, 25.0, 450.0, 54.0, 86.0, 70.0),
    "C45": ClassSpec(45.0, 35.0, 600.0, 80.0, 89.0, 85.0),
    "C55": ClassSpec(55.0, 42.0, 700.0, 97.0, 92.0, 100.0),
}
CLASS_WEIGHTS = {"C30": 0.30, "C45": 0.45, "C55": 0.25}
RATED_RPM = 1480.0
RATED_VOLTAGE = 415.0

OEM_MODELS: dict[str, dict[str, str]] = {
    "AeroTun": {"C30": "AeroTun JF-30/4R", "C45": "AeroTun JF-45/4R", "C55": "AeroTun JF-55/4R"},
    "Ventora": {"C30": "Ventora TBR-800", "C45": "Ventora TBR-1000", "C55": "Ventora TBR-1120"},
}

# OEM time-based PM schedules: (task, interval_months, interval_run_hours, needs_work_at_height)
PM_SCHEDULES: dict[str, list[tuple[str, int, float | None, bool]]] = {
    "AeroTun": [
        ("Visual inspection and cleaning", 3, None, True),
        ("Bearing regreasing", 6, 2000.0, True),
        ("Fastener torque and AV-mount check", 6, None, True),
        ("Insulation resistance test", 12, None, False),
        ("Motor terminal box inspection", 12, None, True),
        ("Impeller balance check", 24, None, True),
        ("Bearing replacement", 60, 20000.0, True),
    ],
    "Ventora": [
        ("General inspection and cleaning", 6, None, True),
        ("Bearing regreasing", 4, 1500.0, True),
        ("Mounting bolts torque check", 12, None, True),
        ("Insulation resistance test", 6, None, False),
        ("Control panel and starter check", 6, None, False),
        ("Bearing replacement", 48, 25000.0, True),
    ],
}


@dataclass
class Baseline:
    """Healthy per-fan characteristics. Mutable: repairs reset components."""

    c1: dict[str, float]            # 1x amplitude per bearing (mm/s)
    c2: dict[str, float]            # 2x amplitude per bearing
    bb: dict[str, float]            # broadband (bearing/flow) component
    load_factor: float
    phase_asym: tuple[float, float, float]
    rise_bearing: dict[str, float]  # steady-state bearing rise over ambient (K)
    tau_bearing_min: float
    rise_winding: float
    tau_winding_min: float
    airflow_factor: float
    dp_factor: float
    ir_mohm: float
    noise_dba: float
    runup_s: float
    coastdown_s: float
    inrush_mult: float


@dataclass
class FanSpec:
    fan_id: str
    index: int
    station: str
    line: str
    tunnel_section: str
    capacity_class: str
    oem: str
    oem_model: str
    install_date: dt.date
    baseline: Baseline
    pm_schedule: list[tuple[str, int, float | None, bool]] = field(default_factory=list)

    @property
    def spec(self) -> ClassSpec:
        return CLASSES[self.capacity_class]

    def row(self) -> dict:
        s = self.spec
        return {
            "fan_id": self.fan_id,
            "station": self.station,
            "line": self.line,
            "tunnel_section": self.tunnel_section,
            "capacity_class": self.capacity_class,
            "rated_power_kw": s.power_kw,
            "rated_airflow_m3s": s.airflow_m3s,
            "rated_diff_pressure_pa": s.diff_pressure_pa,
            "rated_rpm": RATED_RPM,
            "rated_voltage_v": RATED_VOLTAGE,
            "rated_current_a": s.current_a,
            "oem_model": self.oem_model,
            "install_date": self.install_date.isoformat(),
        }


def _lognorm(rng: np.random.Generator, median: float, sigma: float) -> float:
    return float(median * math.exp(rng.normal(0.0, sigma)))


def new_bearing(rng: np.random.Generator, which: str) -> tuple[float, float, float]:
    """(c1, c2, bb) for a freshly fitted / healthy bearing position."""
    scale = 1.0 if which == "DE" else 0.75
    return (
        _lognorm(rng, 0.9 * scale, 0.3),
        _lognorm(rng, 0.35 * scale, 0.3),
        _lognorm(rng, 0.8 if which == "DE" else 0.7, 0.25),
    )


def make_baseline(rng: np.random.Generator, spec: ClassSpec) -> Baseline:
    de, nde = new_bearing(rng, "DE"), new_bearing(rng, "NDE")
    soft_starter = rng.random() < 0.6
    return Baseline(
        c1={"DE": de[0], "NDE": nde[0]},
        c2={"DE": de[1], "NDE": nde[1]},
        bb={"DE": de[2], "NDE": nde[2]},
        load_factor=float(rng.uniform(0.78, 0.92)),
        phase_asym=tuple(float(x) for x in rng.normal(0.0, 0.008, 3)),
        rise_bearing={"DE": float(rng.uniform(18, 28)), "NDE": float(rng.uniform(15, 25))},
        tau_bearing_min=float(rng.uniform(20, 35)),
        rise_winding=float(rng.uniform(45, 65)),
        tau_winding_min=float(rng.uniform(15, 25)),
        airflow_factor=float(rng.uniform(0.95, 1.03)),
        dp_factor=float(rng.uniform(0.92, 1.05)),
        ir_mohm=float(np.clip(_lognorm(rng, 1500.0, 0.4), 300.0, 5000.0)),
        noise_dba=float(spec.noise_dba + rng.normal(0.0, 1.5)),
        runup_s=float(rng.uniform(6.0, 11.0)) * (1.0 if soft_starter else 0.6),
        coastdown_s=float(spec.coastdown_s * rng.uniform(0.85, 1.15)),
        inrush_mult=float(rng.uniform(2.8, 3.8) if soft_starter else rng.uniform(5.5, 6.8)),
    )


def build_fleet(n_fans: int, seed: int) -> list[FanSpec]:
    rng = np.random.default_rng([seed, 0])
    sections: list[tuple[str, str, str]] = []
    for line, stations in CORRIDORS:
        sections += [(line, a, b) for a, b in zip(stations, stations[1:])]
    needed = math.ceil(n_fans / FANS_PER_SECTION)
    if needed > len(sections):
        raise ValueError(f"at most {len(sections) * FANS_PER_SECTION} fans supported")

    fans: list[FanSpec] = []
    classes, weights = list(CLASS_WEIGHTS), list(CLASS_WEIGHTS.values())
    for line, a, b in sections[:needed]:
        cls = str(rng.choice(classes, p=weights))
        oem = str(rng.choice(list(OEM_MODELS)))
        install = dt.date(int(rng.integers(2006, 2022)), int(rng.integers(1, 13)), int(rng.integers(1, 29)))
        for tunnel in ("UP-1", "UP-2", "DN-1", "DN-2"):
            if len(fans) == n_fans:
                break
            i = len(fans)
            fan_rng = np.random.default_rng([seed, 2, i])
            direction = tunnel[:2]
            fans.append(FanSpec(
                fan_id=f"TBF-{i + 1:03d}",
                index=i,
                station=a,
                line=line,
                tunnel_section=f"{direction} {a}–{b} ({tunnel})",
                capacity_class=cls,
                oem=oem,
                oem_model=OEM_MODELS[oem][cls],
                install_date=install,
                baseline=make_baseline(fan_rng, CLASSES[cls]),
                pm_schedule=list(PM_SCHEDULES[oem]),
            ))
    return fans
