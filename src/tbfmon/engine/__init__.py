"""tbfmon analytics engine — layered, explainable condition assessment (DESIGN.md §9).

The engine reads only the database (runs, maintenance, failures, limits). It must
never read simulator ground truth (enforced by the isolation tests).
"""

from .config import EngineConfig, load_engine_config
from .data import FleetData, load_fleet

__all__ = ["EngineConfig", "load_engine_config", "FleetData", "load_fleet"]
