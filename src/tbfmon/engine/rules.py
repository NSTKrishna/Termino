"""Layer 5a — transparent fault-signature rules.

Evidence for one feature: e(z) = clip((z − 2) / 4, 0, 1) on the bad-direction z
(z = 2 → 0, z = 6 → 1). Where the fan's own baseline may already contain the fault
(fault present before data started), peer evidence counts at 70 %:
ev = max(e(z_fan), 0.7 · e(z_peer)).

| mode | primary evidence | supporting evidence |
|---|---|---|
| impeller_imbalance | 1× ↑ at DE/NDE, and 1× dominates overall RMS | — |
| misalignment_looseness | 2× ↑ at DE or 2×/1× ↑ | run-to-run scatter, DE temperature ↑ |
| bearing_wear (per bearing) | broadband ↑ (vibration not explained by 1×/2×) | bearing temp ↑, noise ↑, coast-down ↓ |
| lubrication_loss (per bearing) | bearing temperature rise ↑ | noise ↑, coast-down ↓ |
| insulation_degradation | IR@40 °C ↓ (or IR limit WARN/ALARM); if IR not measured recently, 0.7 × current-imbalance evidence | current imbalance ↑, winding rise ↑ |
| airflow_restriction | airflow ↓ or ΔP ↑ (at rated speed) | current ↓, noise ↑ |

confidence = 0.75 · primary + 0.25 · mean(supporting) (capped at 1). There is no
axial vibration channel in the data model, so misalignment relies on 2× only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .features import FEATURES

MODES = ["bearing_wear", "impeller_imbalance", "misalignment_looseness", "insulation_degradation",
         "airflow_restriction", "lubrication_loss"]


@dataclass
class ModeScore:
    mode: str
    confidence: float
    location: str | None = None
    reasons: list[str] = field(default_factory=list)


def _e(z: float) -> float:
    return 0.0 if z is None or np.isnan(z) else float(np.clip((z - 2.0) / 4.0, 0.0, 1.0))


class _Ev:
    """Evidence helper bound to one assessed run."""

    def __init__(self, row: pd.Series):
        self.row = row

    def ev(self, f: str) -> float:
        return max(_e(self.row.get(f"zb_{f}", np.nan)), 0.7 * _e(self.row.get(f"pzb_{f}", np.nan)))

    def why(self, f: str) -> str:
        feat = FEATURES[f]
        x, b = self.row.get(f), self.row.get(f"b_{f}")
        z, pz = self.row.get(f"z_{f}"), self.row.get(f"pz_{f}")
        unit, show = feat.unit, (lambda v: v)
        if f == "ir40_log":  # explain insulation resistance in MΩ, not log units
            unit, show = "MΩ", (lambda v: 10**v)
        txt = f"{feat.label.replace('log10 ', '')} {show(x):.3g} {unit}" if pd.notna(x) else feat.label
        kts = self.row.get(f"kts_{f}")
        if pd.notna(kts) and pd.notna(self.row.get("ts")) and kts != self.row["ts"]:
            txt += f" (measured {pd.Timestamp(kts).date()})"
        if pd.notna(b) and pd.notna(z) and _e(self.row.get(f"zb_{f}", np.nan)) > 0:
            return f"{txt} vs own baseline {show(b):.3g} {unit} (z={z:+.1f})"
        if pd.notna(pz):
            return f"{txt} vs class peers (z={pz:+.1f})"
        return txt


def _combine(primary: float, supports: list[float]) -> float:
    if primary <= 0:
        return 0.0
    return float(min(1.0, 0.75 * primary + 0.25 * (np.mean(supports) if supports else 0.0)))


def score_run(row: pd.Series, limit_status: dict[str, str] | None = None) -> list[ModeScore]:
    """All modes with confidence > 0, highest first. `row` = features + z + peer z columns."""
    ev = _Ev(row)
    limit_status = limit_status or {}
    out: list[ModeScore] = []

    # impeller imbalance
    loc = "de" if ev.ev("vib_de_1x") >= ev.ev("vib_nde_1x") else "nde"
    p = ev.ev(f"vib_{loc}_1x")
    share = np.nan
    c1, rms = row.get(f"vib_{loc}_1x"), row.get(f"vib_{loc}_rms")
    if pd.notna(c1) and pd.notna(rms) and rms > 0:
        share = float(min(1.0, (c1 / rms) ** 2))
    conf = p * (1.0 if not np.isnan(share) and share >= 0.5 else 0.6)
    if conf > 0:
        r = [ev.why(f"vib_{loc}_1x")]
        if not np.isnan(share):
            r.append(f"1× carries {share * 100:.0f}% of {loc.upper()} vibration energy")
        out.append(ModeScore("impeller_imbalance", round(conf, 3), None, r))

    # misalignment / looseness
    p = max(ev.ev("vib_de_2x"), ev.ev("vib_de_2x_1x"))
    cv = row.get("vib_de_cv3", np.nan)
    scatter = float(np.clip((cv - 0.15) / 0.25, 0, 1)) if pd.notna(cv) else 0.0
    conf = _combine(p, [scatter, ev.ev("temp_de_rise")])
    if conf > 0:
        r = [ev.why("vib_de_2x" if ev.ev("vib_de_2x") >= ev.ev("vib_de_2x_1x") else "vib_de_2x_1x")]
        if scatter > 0:
            r.append(f"erratic DE vibration run-to-run (CV {cv * 100:.0f}%)")
        out.append(ModeScore("misalignment_looseness", round(conf, 3), "DE", r))

    # bearing wear & lubrication loss, per bearing — keep the stronger bearing
    for mode, prim, sup in (
        ("bearing_wear", "vib_{l}_bb", ["temp_{l}_rise", "noise", "coastdown"]),
        ("lubrication_loss", "temp_{l}_rise", ["noise", "coastdown"]),
    ):
        best: ModeScore | None = None
        for loc in ("de", "nde"):
            pf = prim.format(l=loc)
            conf = _combine(ev.ev(pf), [ev.ev(s.format(l=loc)) for s in sup])
            if conf > 0 and (best is None or conf > best.confidence):
                r = [ev.why(pf)] + [ev.why(s.format(l=loc)) for s in sup if ev.ev(s.format(l=loc)) > 0]
                best = ModeScore(mode, round(conf, 3), loc.upper(), r)
        if best:
            out.append(best)

    # insulation degradation
    p = ev.ev("ir40_log")
    ir_status = limit_status.get("insulation_resistance_mohm")
    if ir_status in ("WARN", "ALARM"):
        p = max(p, 0.7 if ir_status == "WARN" else 1.0)
    ir_unknown = pd.isna(row.get("ir40_log"))  # IR not measured within the carry-forward window
    if ir_unknown:  # rising current imbalance alone points at the winding, at reduced weight
        p = max(p, 0.7 * ev.ev("current_imbalance"))
    conf = _combine(p, [ev.ev("current_imbalance"), ev.ev("winding_rise")])
    if conf > 0:
        r = ([ev.why("ir40_log")] if not ir_unknown else
             ["IR not measured recently — current imbalance alone; measure IR to confirm"])
        r += [f"IR limit {ir_status}"] if ir_status in ("WARN", "ALARM") else []
        r += [ev.why(f) for f in ("current_imbalance", "winding_rise") if ev.ev(f) > 0]
        out.append(ModeScore("insulation_degradation", round(conf, 3), None, r))

    # airflow restriction
    p = max(ev.ev("airflow_pct"), ev.ev("dp_pct"))
    z_i = row.get("z_current_pct", np.nan)
    cur_down = _e(-z_i) if pd.notna(z_i) else 0.0
    conf = _combine(p, [cur_down, ev.ev("noise")])
    if conf > 0:
        r = [ev.why(f) for f in ("airflow_pct", "dp_pct") if ev.ev(f) > 0]
        if cur_down > 0:
            r.append(ev.why("current_pct") + " (lower load)")
        out.append(ModeScore("airflow_restriction", round(conf, 3), None, r))

    return sorted(out, key=lambda m: -m.confidence)


def scatter_cv(feats: pd.DataFrame) -> pd.Series:
    """Causal coefficient of variation of DE RMS over the last 3 runs of the segment."""
    g = feats.groupby(["fan_id", "seg_vib"], sort=False)["vib_de_rms"]
    mean = g.transform(lambda s: s.rolling(3, min_periods=3).mean())
    std = g.transform(lambda s: s.rolling(3, min_periods=3).std())
    return std / mean
