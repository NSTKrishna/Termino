"""Layering (DESIGN.md §11): engine ← services ← CLI. A web dashboard could replace the CLI
without touching the engine or the services, so neither may import Typer, Rich or the CLI."""

from __future__ import annotations

import re
from pathlib import Path

import tbfmon

SRC = Path(tbfmon.__file__).parent
FORBIDDEN = re.compile(r"^\s*(from|import)\s+(typer|rich|click|tbfmon\.cli\w*|\.+cli\w*)\b", re.M)


def test_engine_and_services_are_ui_free():
    offenders = []
    for pkg in ("engine", "services", "evaluation"):
        for p in (SRC / pkg).rglob("*.py"):
            if FORBIDDEN.search(p.read_text(encoding="utf-8")):
                offenders.append(str(p.relative_to(SRC)))
    assert offenders == []


def test_engine_does_not_depend_on_services():
    for p in (SRC / "engine").rglob("*.py"):
        assert not re.search(r"(from|import)\s+[.\w]*services", p.read_text(encoding="utf-8")), p
