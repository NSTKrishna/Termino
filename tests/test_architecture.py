"""Layering: only the CLI and render.py may use Typer or Rich. The data layer (models, db,
sources, ingest, limits, pm, validate, views, simulate, services) stays UI-free, so another
front end could reuse it without changes."""

from __future__ import annotations

import re
from pathlib import Path

import tbfmon

SRC = Path(tbfmon.__file__).parent
UI_MODULES = {"cli.py", "render.py", "__main__.py"}
FORBIDDEN = re.compile(r"^\s*(from|import)\s+(typer|rich|click|tbfmon\.cli\w*|\.+cli\w*)\b", re.M)


def test_data_layer_is_ui_free():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if str(p.relative_to(SRC)) not in UI_MODULES and FORBIDDEN.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
