"""Engine configuration (engine.yaml). See DESIGN.md §9."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeaturesCfg(_M):
    thermal_tau_min: float = 25.0
    ir_reference_c: float = 40.0


class CleaningCfg(_M):
    spike_k: float = 5.0
    phase_tolerance: float = 0.30


class BaselineCfg(_M):
    n_runs: int = Field(5, ge=2)
    min_runs: int = Field(3, ge=2)
    carry_forward_days: int = 180


class LabelsCfg(_M):
    exclude_days_before_failure: int = 180
    exclude_days_before_corrective: int = 90
    exclude_days_before_abnormal_pm: int = 60
    abnormal_findings: list[str] = Field(default_factory=list)


class DriftCfg(_M):
    ewma_lambda: float = Field(0.3, gt=0, le=1)
    ewma_limit: float = 2.5
    cusum_k: float = 0.5
    cusum_h: float = 5.0
    z_watch: float = 3.0
    z_cap: float = 8.0


class PeersCfg(_M):
    window_days: int = 365
    min_peers: int = 3


class AnomalyCfg(_M):
    n_estimators: int = 200
    flag_percentile: float = 0.98
    top_k: int = 3


class SupervisedCfg(_M):
    lookback_days: int = 180
    ambiguous_days: int = 365
    n_splits: int = 5
    n_estimators: int = 300
    max_depth: int = 6
    min_samples_leaf: int = 3


class FaultModesCfg(_M):
    top_k: int = 3
    rule_weight: float = Field(0.6, ge=0, le=1)
    min_confidence: float = 0.5


class TrendCfg(_M):
    min_runs: int = 4
    min_span_days: int = 60
    max_runs: int = 10
    confidence: float = 0.90


class HealthStatusCfg(_M):
    healthy: float = 80
    watch: float = 60
    degraded: float = 40


class HealthIndexCfg(_M):
    weights: dict[str, float]
    status: HealthStatusCfg = HealthStatusCfg()
    alarm_is_critical: bool = True
    warn_is_watch: bool = True

    @model_validator(mode="after")
    def _weights(self) -> "HealthIndexCfg":
        expected = {"limits", "drift", "anomaly", "fault_mode", "trend", "peers"}
        if set(self.weights) != expected:
            raise ValueError(f"health_index.weights must have exactly {sorted(expected)}")
        if abs(sum(self.weights.values()) - 100.0) > 1e-6:
            raise ValueError("health_index.weights must sum to 100")
        return self


class RecommenderCfg(_M):
    horizon_days: int = 365
    urgent_within_days: int = 7
    inspect_within_days: int = 30
    watch_inspect_within_days: int = 60
    stale_after_days: int = 120
    defer_factor: float = 1.0
    max_interval_factor: float = 2.0
    deferrable_tasks: list[str]
    man_hours: dict[str, float]
    task_modes: dict[str, list[str]]
    inspections: dict[str, list[str]]
    corrective: dict[str, list[str]]


class EvaluationCfg(_M):
    oem_catches: dict[str, list[str]]
    pre_onset_grace_days: int = 30


class EngineConfig(_M):
    version: int
    seed: int = 42
    features: FeaturesCfg = FeaturesCfg()
    cleaning: CleaningCfg = CleaningCfg()
    baseline: BaselineCfg = BaselineCfg()
    labels: LabelsCfg = LabelsCfg()
    drift: DriftCfg = DriftCfg()
    peers: PeersCfg = PeersCfg()
    anomaly: AnomalyCfg = AnomalyCfg()
    supervised: SupervisedCfg = SupervisedCfg()
    fault_modes: FaultModesCfg = FaultModesCfg()
    trend: TrendCfg = TrendCfg()
    health_index: HealthIndexCfg
    recommender: RecommenderCfg
    evaluation: EvaluationCfg


def default_engine_text() -> str:
    return resources.files("tbfmon").joinpath("data/engine.yaml").read_text(encoding="utf-8")


def load_engine_config(path: Path | None = None) -> EngineConfig:
    text = path.read_text(encoding="utf-8") if path and path.exists() else default_engine_text()
    return EngineConfig.model_validate(yaml.safe_load(text))
