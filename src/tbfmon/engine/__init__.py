"""tbfmon analytics engine — layered, explainable condition assessment (DESIGN.md §9).

Layers: 0 clean → 1 limit check → 2 features → 3 baseline/drift/peers →
4 anomaly (Isolation Forest) → 5 fault modes (rules + RandomForest) →
6 trend / time-to-ALARM → 7 health index → 8 recommendation.

The engine reads only the database (runs, maintenance, failures, limits). It must
never read simulator ground truth (enforced by the isolation tests).
"""

from .config import EngineConfig, load_engine_config
from .data import FleetData, load_fleet
from .pipeline import Assessment, assess, explain_run, recommend_all

__all__ = ["EngineConfig", "load_engine_config", "FleetData", "load_fleet", "Assessment", "assess",
           "explain_run", "recommend_all"]
