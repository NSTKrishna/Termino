"""Terminal output: Rich tables for humans, JSON for machines. Terminal only (DESIGN.md §0)."""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
from typing import Any, Iterable

import numpy as np
from rich.console import Console
from rich.table import Table

out = Console()                  # tables → stdout
err = Console(stderr=True)       # messages → stderr (keeps --json stdout clean)

STATUS_STYLE = {
    "OK": "green", "PASS": "green", "WARN": "yellow", "DUE SOON": "yellow", "ALARM": "bold red",
    "FAIL": "bold red", "OVERDUE": "red", "NA": "dim", "NO RECORD": "dim", "SUSPECT": "magenta",
}
SPARKS = "▁▂▃▄▅▆▇█"


def _default(o: Any):
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    if isinstance(o, np.generic):
        return o.item()
    if hasattr(o, "value"):
        return o.value
    return str(o)


def _nan_to_none(o: Any):
    if isinstance(o, float) and math.isnan(o):
        return None
    if isinstance(o, dict):
        return {k: _nan_to_none(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_nan_to_none(v) for v in o]
    return o


def emit_json(obj: Any) -> None:
    sys.stdout.write(json.dumps(_nan_to_none(obj), default=_default, indent=2, ensure_ascii=False) + "\n")


def styled(status: str) -> str:
    style = STATUS_STYLE.get(status, "")
    return f"[{style}]{status}[/]" if style else status


def sparkline(values: Iterable[float | None]) -> str:
    vals = [None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v) for v in values]
    present = [v for v in vals if v is not None]
    if not present:
        return ""
    lo, hi = min(present), max(present)
    span = hi - lo or 1.0
    return "".join("·" if v is None else SPARKS[min(len(SPARKS) - 1, int((v - lo) / span * (len(SPARKS) - 1) + 0.5))]
                   for v in vals)


def fmt(v: Any, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if isinstance(v, float):
        if abs(v) >= 1e6:
            return f"{v:.3g}"
        text = f"{v:.{nd}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(v)


def table(title: str | None = None, *columns: str | tuple[str, dict], **kw) -> Table:
    t = Table(title=title, title_justify="left", header_style="bold", **kw)
    for c in columns:
        name, opts = (c, {}) if isinstance(c, str) else c
        t.add_column(name, **opts)
    return t


HEALTH_STYLE = {"Healthy": "green", "Watch": "yellow", "Degraded": "dark_orange", "Critical": "bold red",
                "No data": "dim"}
SEVERITY_STYLE = {"INFO": "cyan", "WARN": "yellow", "ALARM": "bold red", "CRITICAL": "bold white on red"}
PARAM_STYLE = {"OK": "green", "WARN": "yellow", "ALARM": "bold red", "SUSPECT": "magenta", "NA": "dim",
               "not measured": "dim", "no limit": "dim"}


def health_label(status: str) -> str:
    return f"[{HEALTH_STYLE.get(status, '')}]{status}[/]"


def severity_label(sev: str) -> str:
    return f"[{SEVERITY_STYLE.get(sev, '')}]{sev}[/]"


def param_label(status: str) -> str:
    return f"[{PARAM_STYLE.get(status, '')}]{status}[/]"


def hi_bar(hi: float | None, status: str, width: int = 20, compact: bool = False) -> str:
    """Health index bar, e.g. '██████████████░░░░░░ 72/100' (compact: '███░░ 72')."""
    if hi is None:
        return "[dim]no data[/]"
    filled = int(round(max(0.0, min(100.0, hi)) / 100 * width))
    style = HEALTH_STYLE.get(status, "")
    return f"[{style}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/] {hi:.0f}" + ("" if compact else "/100")


def conf_bar(conf: float, width: int = 10) -> str:
    filled = int(round(max(0.0, min(1.0, conf)) * width))
    return f"{'▰' * filled}{'▱' * (width - filled)} {conf * 100:.0f}%"
