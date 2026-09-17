"""Source interface. See DESIGN.md §5.

A source yields `RawRecord`s: untyped dicts tagged with a kind. Sources do no
validation; `tbfmon.ingest` sanitises, validates and upserts every record, so
all sources share the same rules and idempotency guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Literal, Protocol, runtime_checkable

Kind = Literal["fan", "pm_task", "test_run", "maintenance", "failure"]

# Dependency order: fans must exist before anything that references them.
KIND_ORDER: tuple[Kind, ...] = ("fan", "pm_task", "test_run", "maintenance", "failure")


@dataclass(frozen=True)
class RawRecord:
    kind: Kind
    data: dict[str, Any]
    origin: str  # human-readable provenance, e.g. "test_runs.csv:17"


@runtime_checkable
class Source(Protocol):
    name: str

    def records(self) -> Iterator[RawRecord]:
        """Yield records in KIND_ORDER."""
        ...


class GroundTruthRefused(ValueError):
    """Raised when asked to read simulator ground truth (evaluator-only data)."""
