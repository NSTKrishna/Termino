"""Fault modes: episode generation and symptom signatures. See DESIGN.md §4."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from ..models import FailureMode

# mode -> (relative weight, onset→failure duration range in days, exponent k range)
MODE_PARAMS: dict[FailureMode, tuple[float, tuple[int, int], tuple[float, float]]] = {
    FailureMode.BEARING_WEAR: (0.22, (180, 540), (1.8, 3.0)),
    FailureMode.IMPELLER_IMBALANCE: (0.18, (150, 450), (1.2, 2.0)),
    FailureMode.MISALIGNMENT_LOOSENESS: (0.15, (90, 360), (1.3, 2.2)),
    FailureMode.INSULATION_DEGRADATION: (0.15, (240, 720), (1.2, 2.0)),
    FailureMode.AIRFLOW_RESTRICTION: (0.15, (90, 300), (1.0, 1.6)),
    FailureMode.LUBRICATION_LOSS: (0.15, (60, 240), (1.5, 2.5)),
}
BASE_RATE_PER_FAN_YEAR = 0.22
SUDDEN_RATE_PER_FAN_YEAR = 0.03

SUDDEN_CAUSES = [
    ("Main contactor coil burnt out", "Replaced contactor"),
    ("Control cable damaged by rodents", "Re-terminated and replaced control cable section"),
    ("Overload relay faulty (nuisance trip)", "Replaced overload relay"),
    ("Soft-starter thyristor failure", "Replaced soft-starter power module"),
]

# Parameters whose deviation characterises each mode. `{L}` = affected bearing.
SIGNATURES: dict[FailureMode, list[tuple[str, str]]] = {
    FailureMode.BEARING_WEAR: [
        ("vib_{l}_rms_mms", "increase"), ("temp_bearing_{l}_c", "increase"),
        ("noise_dba", "increase"), ("coastdown_time_s", "decrease"),
    ],
    FailureMode.IMPELLER_IMBALANCE: [
        ("vib_de_1x_mms", "increase"), ("vib_nde_1x_mms", "increase"), ("vib_de_rms_mms", "increase"),
    ],
    FailureMode.MISALIGNMENT_LOOSENESS: [
        ("vib_de_2x_mms", "increase"), ("vib_de_rms_mms", "increase"), ("temp_bearing_de_c", "increase"),
    ],
    FailureMode.INSULATION_DEGRADATION: [
        ("insulation_resistance_mohm", "decrease"), ("current_imbalance_pct", "increase"),
        ("temp_winding_c", "increase"),
    ],
    FailureMode.AIRFLOW_RESTRICTION: [
        ("airflow_m3s", "decrease"), ("diff_pressure_pa", "increase"),
        ("current_l1_a", "decrease"), ("noise_dba", "increase"),
    ],
    FailureMode.LUBRICATION_LOSS: [
        ("temp_bearing_{l}_c", "increase"), ("vib_{l}_rms_mms", "increase"),
        ("noise_dba", "increase"), ("coastdown_time_s", "decrease"),
    ],
}

ROOT_CAUSE: dict[FailureMode, str] = {
    FailureMode.BEARING_WEAR: "{L} bearing outer-race spalling; fatigue aggravated by false brinelling during long idle periods",
    FailureMode.IMPELLER_IMBALANCE: "Impeller imbalance from tunnel dust/brake-dust build-up on blades",
    FailureMode.MISALIGNMENT_LOOSENESS: "Loose mounting bolts and degraded anti-vibration mounts causing misalignment",
    FailureMode.INSULATION_DEGRADATION: "Stator winding insulation breakdown (moisture ingress during idle monsoon months)",
    FailureMode.AIRFLOW_RESTRICTION: "Silencer/guard clogged with debris; damper partially stuck",
    FailureMode.LUBRICATION_LOSS: "{L} bearing grease dried/hardened; bearing overheated and seized",
}
REPAIR: dict[FailureMode, tuple[str, list[str], str]] = {
    FailureMode.BEARING_WEAR: ("Replace {L} bearing", ["{L} bearing 6314-C3", "grease LGHP2"], "Replaced {L} bearing, regreased"),
    FailureMode.IMPELLER_IMBALANCE: ("Impeller cleaning and dynamic balancing", ["balance weights"], "Cleaned impeller, balanced in situ to G6.3"),
    FailureMode.MISALIGNMENT_LOOSENESS: ("Re-align motor and replace AV mounts", ["AV mount x4", "HT bolts M16 x8"], "Replaced AV mounts, re-torqued, realigned"),
    FailureMode.INSULATION_DEGRADATION: ("Motor rewind", ["motor rewind (class F)"], "Motor removed, rewound and re-installed"),
    FailureMode.AIRFLOW_RESTRICTION: ("Clean silencer/guards and free damper", ["damper actuator linkage"], "Cleared silencer and guards, freed damper"),
    FailureMode.LUBRICATION_LOSS: ("Replace seized {L} bearing", ["{L} bearing 6314-C3", "grease LGHP2"], "Replaced seized {L} bearing, regreased both ends"),
}


@dataclass
class Episode:
    fault_id: str
    fan_id: str
    mode: FailureMode
    location: str | None          # "DE" / "NDE" for bearing modes
    onset: dt.date
    planned_failure: dt.date
    k: float
    end_date: dt.date | None = None
    failure_date: dt.date | None = None
    outcome: str = "ongoing"      # failed | resolved_by_pm | ongoing
    notes: list[str] = field(default_factory=list)

    @property
    def sudden(self) -> bool:
        return self.mode is FailureMode.OTHER

    def active(self, d: dt.date) -> bool:
        return self.onset <= d and (self.end_date is None or d < self.end_date)

    def severity(self, d: dt.date) -> float:
        if not self.active(d):
            return 0.0
        span = (self.planned_failure - self.onset).days
        if span <= 0:
            return 1.0
        x = min(max((d - self.onset).days / span, 0.0), 1.0)
        return x**self.k

    def signature(self) -> list[tuple[str, str]]:
        loc = (self.location or "DE").lower()
        return [(p.format(l=loc), d) for p, d in SIGNATURES.get(self.mode, [])]


def generate_episodes(
    rng: np.random.Generator,
    fan_id: str,
    install: dt.date,
    start: dt.date,
    end: dt.date,
    counter: list[int],
) -> list[Episode]:
    years = (end - start).days / 365.25
    age = max((start - install).days / 365.25, 0.0)
    lam = BASE_RATE_PER_FAN_YEAR * years * (1.0 + age / 15.0)
    modes = list(MODE_PARAMS)
    weights = np.array([MODE_PARAMS[m][0] for m in modes])
    weights = weights / weights.sum()

    episodes: list[Episode] = []
    for _ in range(int(rng.poisson(lam))):
        mode = modes[int(rng.choice(len(modes), p=weights))]
        _, (dlo, dhi), (klo, khi) = MODE_PARAMS[mode]
        duration = int(rng.integers(dlo, dhi + 1))
        # Onset may precede the window: the fan can already be degrading at start.
        earliest = start - dt.timedelta(days=int(duration * 0.3))
        onset = earliest + dt.timedelta(days=int(rng.integers(0, (end - earliest).days)))
        failure = onset + dt.timedelta(days=duration)
        if any(e.mode == mode and e.onset <= failure and onset <= e.planned_failure for e in episodes):
            continue  # one episode of a given mode at a time
        location = None
        if mode in (FailureMode.BEARING_WEAR, FailureMode.LUBRICATION_LOSS):
            location = "DE" if rng.random() < 0.6 else "NDE"
        elif mode is FailureMode.MISALIGNMENT_LOOSENESS:
            location = "DE"
        counter[0] += 1
        episodes.append(Episode(f"F{counter[0]:04d}", fan_id, mode, location, onset, failure,
                                float(rng.uniform(klo, khi))))

    for _ in range(int(rng.poisson(SUDDEN_RATE_PER_FAN_YEAR * years))):
        d = start + dt.timedelta(days=int(rng.integers(0, (end - start).days)))
        counter[0] += 1
        episodes.append(Episode(f"F{counter[0]:04d}", fan_id, FailureMode.OTHER, None, d, d, 1.0))
    return sorted(episodes, key=lambda e: (e.onset, e.fault_id))


@dataclass
class Effects:
    """Accumulated deviations from baseline for one run."""

    c1: dict[str, float] = field(default_factory=lambda: {"DE": 0.0, "NDE": 0.0})
    c2: dict[str, float] = field(default_factory=lambda: {"DE": 0.0, "NDE": 0.0})
    bb: dict[str, float] = field(default_factory=lambda: {"DE": 0.0, "NDE": 0.0})
    temp: dict[str, float] = field(default_factory=lambda: {"DE": 0.0, "NDE": 0.0})
    winding: float = 0.0
    current_mult: float = 1.0
    imbalance_pct: float = 0.0
    ir_mult: float = 1.0
    airflow_mult: float = 1.0
    dp_mult: float = 1.0
    noise: float = 0.0
    coast_mult: float = 1.0
    runup_mult: float = 1.0
    scatter: float = 1.0


def apply(effects: Effects, ep: Episode, s: float) -> None:
    """Add the signature of episode `ep` at severity `s` ∈ [0, 1]."""
    if s <= 0:
        return
    L = ep.location or "DE"
    O = "NDE" if L == "DE" else "DE"
    m = ep.mode
    if m is FailureMode.BEARING_WEAR:
        effects.bb[L] += 4.5 * s**1.2
        effects.bb[O] += 0.8 * s
        effects.c1[L] += 0.3 * s
        effects.c2[L] += 0.25 * s
        effects.temp[L] += 14.0 * s**2
        effects.noise += 6.0 * s
        effects.coast_mult *= 1 - 0.15 * s
        effects.current_mult *= 1 + 0.03 * s
        effects.runup_mult *= 1 + 0.10 * s
    elif m is FailureMode.IMPELLER_IMBALANCE:
        effects.c1["DE"] += 4.0 * s
        effects.c1["NDE"] += 2.8 * s
        effects.bb["DE"] += 0.3 * s
        effects.bb["NDE"] += 0.3 * s
        effects.noise += 3.0 * s
        effects.current_mult *= 1 + 0.02 * s
    elif m is FailureMode.MISALIGNMENT_LOOSENESS:
        effects.c2["DE"] += 3.0 * s
        effects.c2["NDE"] += 1.2 * s
        effects.c1["DE"] += 1.2 * s
        effects.scatter *= 1 + 3.0 * s
        effects.temp["DE"] += 6.0 * s
        effects.noise += 2.5 * s
    elif m is FailureMode.INSULATION_DEGRADATION:
        effects.ir_mult *= float(np.exp(-5.5 * s))
        if s > 0.6:
            effects.imbalance_pct += 6.0 * (s - 0.6) / 0.4
        effects.winding += 10.0 * s**2
        effects.current_mult *= 1 + 0.02 * s**2
    elif m is FailureMode.AIRFLOW_RESTRICTION:
        effects.airflow_mult *= 1 - 0.32 * s
        effects.dp_mult *= 1 + 0.28 * s
        effects.current_mult *= 1 - 0.07 * s
        effects.noise += 4.0 * s
        effects.bb["DE"] += 0.8 * s**2
        effects.bb["NDE"] += 0.8 * s**2
    elif m is FailureMode.LUBRICATION_LOSS:
        effects.temp[L] += 38.0 * s**1.5
        effects.bb[L] += 1.8 * s**2
        effects.noise += 5.0 * s
        effects.coast_mult *= 1 - 0.25 * s
        effects.current_mult *= 1 + 0.04 * s
        effects.runup_mult *= 1 + 0.12 * s
