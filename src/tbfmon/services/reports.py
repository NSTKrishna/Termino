"""Reports as files: Markdown, static HTML, CSV or JSON (DESIGN.md §0: HTML only as a file).

One document model (a list of blocks) renders to Markdown and to a self-contained HTML
file with inline CSS and no scripts. CSV exports the main table; JSON the full data.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import db
from .alerts import AlertStore
from .analysis import analyze_fan, fleet_health
from .errors import TbfmonError
from .explain import explain_fan
from .plan import build_plan
from .workspace import Workspace

FORMATS = ("md", "html", "csv", "json")


@dataclass
class Doc:
    title: str
    blocks: list[tuple] = field(default_factory=list)   # ("h2", text) | ("p", text) | ("kv", [(k, v)]) | ("table", headers, rows)
    data: dict[str, Any] = field(default_factory=dict)  # full data for JSON
    csv_table: tuple[list[str], list[list[Any]]] = ([], [])

    def h2(self, t: str) -> None:
        self.blocks.append(("h2", t))

    def p(self, t: str) -> None:
        self.blocks.append(("p", t))

    def kv(self, pairs: list[tuple[str, Any]]) -> None:
        self.blocks.append(("kv", pairs))

    def table(self, headers: list[str], rows: list[list[Any]]) -> None:
        self.blocks.append(("table", headers, rows))


def _s(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3g}" if abs(v) < 1000 else f"{v:.0f}"
    return str(v)


def to_markdown(doc: Doc) -> str:
    out = [f"# {doc.title}", ""]
    for b in doc.blocks:
        if b[0] == "h2":
            out += [f"## {b[1]}", ""]
        elif b[0] == "p":
            out += [b[1], ""]
        elif b[0] == "kv":
            out += [f"- **{k}:** {_s(v)}" for k, v in b[1]] + [""]
        elif b[0] == "table":
            headers, rows = b[1], b[2]
            if not rows:
                out += ["_none_", ""]
                continue
            out.append("| " + " | ".join(headers) + " |")
            out.append("|" + "|".join("---" for _ in headers) + "|")
            out += ["| " + " | ".join(_s(c).replace("|", "/").replace("\n", " ") for c in r) + " |" for r in rows]
            out.append("")
    return "\n".join(out)


CSS = """
:root{--bg:#fff;--fg:#1d2327;--muted:#5f6b76;--line:#d9dee3;--head:#f3f5f7;--bad:#b3261e;--warn:#9a6700;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#15191d;--fg:#e6e8ea;--muted:#9aa4ad;--line:#2f363d;--head:#1e252b;
--bad:#ff8a80;--warn:#f2c14e;--ok:#7ee2a8}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;max-width:1100px;
margin:0 auto;padding:24px 16px}
h1{font-size:1.5rem;margin:0 0 4px}h2{font-size:1.15rem;margin:28px 0 8px;border-bottom:1px solid var(--line)}
p,.kv{color:var(--muted)}table{border-collapse:collapse;width:100%;font-size:.9rem;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{background:var(--head)}
.ALARM,.CRITICAL,.Critical{color:var(--bad);font-weight:600}.WARN,.Degraded,.Watch{color:var(--warn);font-weight:600}
.OK,.Healthy{color:var(--ok)}.banner{border:1px solid var(--warn);color:var(--warn);padding:8px 12px;border-radius:6px}
"""
CLASSES = {"ALARM", "CRITICAL", "Critical", "WARN", "Degraded", "Watch", "OK", "Healthy"}


def to_html(doc: Doc) -> str:
    e = html.escape
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport "
             f"content='width=device-width,initial-scale=1'><title>{e(doc.title)}</title><style>{CSS}</style></head><body>",
             f"<h1>{e(doc.title)}</h1>"]
    for b in doc.blocks:
        if b[0] == "h2":
            parts.append(f"<h2>{e(b[1])}</h2>")
        elif b[0] == "p":
            cls = " class=banner" if b[1].startswith("⚠") else ""
            parts.append(f"<p{cls}>{e(b[1])}</p>")
        elif b[0] == "kv":
            parts.append("<div class=kv>" + "<br>".join(f"<b>{e(k)}:</b> {e(_s(v))}" for k, v in b[1]) + "</div>")
        elif b[0] == "table":
            headers, rows = b[1], b[2]
            if not rows:
                parts.append("<p><i>none</i></p>")
                continue
            head = "".join(f"<th>{e(h)}</th>" for h in headers)
            body = "".join("<tr>" + "".join(
                f"<td class={e(_s(c))}>{e(_s(c))}</td>" if _s(c) in CLASSES else f"<td>{e(_s(c))}</td>" for c in r)
                + "</tr>" for r in rows)
            parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
    parts.append("</body></html>")
    return "\n".join(parts)


def to_csv(doc: Doc) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    headers, rows = doc.csv_table
    w.writerow(headers)
    w.writerows([["" if c is None else c for c in r] for r in rows])
    return buf.getvalue()


def data_is_simulated(ws: Workspace) -> bool:
    r = ws.conn.execute("SELECT COUNT(*) FROM test_run WHERE source LIKE 'simulator:%' OR source LIKE '%/sim%' "
                        "OR source LIKE 'drill:%' OR source LIKE 'watch:%'").fetchone()[0]
    return r > 0


def _header(doc: Doc, ws: Workspace, as_of: str) -> None:
    placeholders = ws.limits.placeholder_count()
    doc.kv([("Generated", ws.settings.fmt(ws.settings.now())), ("Data as of", as_of),
            ("Workspace", str(ws.home.root))])
    if data_is_simulated(ws):
        doc.p("⚠ Data source includes SIMULATED readings (tbfmon simulate / simulated drills) — not real DMRC data.")
    if placeholders:
        doc.p(f"⚠ {placeholders} permissible limits are placeholders — REPLACE WITH OEM VALUES.")


def fan_report(ws: Workspace, fan: str) -> Doc:
    an = analyze_fan(ws, fan).to_dict()
    ex = explain_fan(ws, an["fan_id"])
    info = db.get_fan(ws.conn, an["fan_id"])
    plan = build_plan(ws, 90, fan=an["fan_id"]).to_dict()
    alerts = AlertStore(ws).list("open", an["fan_id"])
    maint = db.maintenance(ws.conn, an["fan_id"])[-10:]
    fails = db.failures(ws.conn, an["fan_id"])
    doc = Doc(f"Fan condition report — {an['fan_id']}")
    _header(doc, ws, ex["as_of"])
    doc.h2("Summary")
    rec = an["recommendation"]
    doc.kv([("Station", f"{info['station']} ({info['line']} line) · {info['tunnel_section']}"),
            ("Class / OEM", f"{info['capacity_class']} · {info['rated_power_kw']:g} kW · {info['oem_model']}"),
            ("Installed", info["install_date"]), ("Last run", f"#{an['run_id']} {ws.settings.fmt(an['run_ts'])}"),
            ("Health index", f"{an['health_index']:.0f}/100 ({an['status']})"),
            ("Recommended action", f"{rec['action']}" + (f" by {rec['by_date']}" if rec["by_date"] else "")
             + (" — work at height" if rec["needs_work_at_height"] else "")),
            ("Reasons", ", ".join(an["reasons"]) or "—")])
    if rec["tasks"]:
        doc.table(["Task"], [[t] for t in rec["tasks"]])
    doc.h2("Readings vs permissible limits (last run)")
    rows = [[p["label"], p["measured"], p["unit"], p["warn"], p["alarm"], p["deviation_pct"], p["status"]]
            for p in an["params"]]
    doc.table(["Parameter", "Measured", "Unit", "Warn", "Alarm", "Deviation % vs WARN", "Status"], rows)
    doc.csv_table = (["parameter", "measured", "unit", "warn", "alarm", "deviation_pct", "status"],
                     [[p["parameter"], p["measured"], p["unit"], p["warn"], p["alarm"], p["deviation_pct"], p["status"]]
                      for p in an["params"]])
    if an["missing_sensors"]:
        doc.p("Missing sensors on this run: " + ", ".join(an["missing_sensors"]))
    doc.h2("Likely fault modes")
    doc.table(["Mode", "Confidence", "Evidence"],
              [[m["mode"] + (f" ({m['location']})" if m["location"] else ""), f"{m['confidence'] * 100:.0f}%",
                "; ".join(m["reasons"])] for m in an["fault_modes"]])
    doc.h2("Why it was flagged")
    doc.table(["Feature", "Value", "Baseline", "z", "Peer z", "Evidence from"],
              [[c["label"], c["value"], c["baseline"], c["z"], c["peer_z"], ", ".join(c["sources"])]
               for c in ex["contributing_features"]])
    doc.h2("Similar past failures")
    doc.table(["Similarity", "Fan", "Date", "Mode", "Root cause", "Fix"],
              [[s["similarity"], s["fan_id"], s["date"], s["failure_mode"], s["root_cause"], s["fix"]]
               for s in ex["similar_past_failures"]])
    doc.h2("Plan — next 90 days")
    items = [i for st in plan["stations"] for i in st["items"]]
    doc.table(["Due", "Type", "Task", "Work at height", "Man-hours"],
              [[i["due"], i["kind"], i["task"], "yes" if i["needs_work_at_height"] else "no", i["man_hours"]] for i in items])
    if plan["deferred"]:
        doc.table(["Deferred OEM task", "OEM due", "Deferred to", "Evidence"],
                  [[d["task"], d["oem_due"], d["deferred_to"], d["evidence"]] for d in plan["deferred"]])
    doc.h2("Open alerts")
    doc.table(["ID", "Severity", "Title", "First seen"],
              [[a["id"], a["severity"], a["title"], ws.settings.fmt(a["created_at"])] for a in alerts])
    doc.h2("Maintenance history (latest 10)")
    doc.table(["Date", "Type", "Task", "Findings"], [[m["date"], m["type"], m["task"], m["findings"]] for m in maint])
    doc.h2("Failures")
    doc.table(["Date", "Mode", "Root cause", "Fix"], [[f["date"], f["failure_mode"], f["root_cause"], f["fix"]] for f in fails])
    doc.data = {"fan": info, "analysis": an, "explanation": ex, "plan": plan, "open_alerts": alerts,
                "maintenance": maint, "failures": fails}
    return doc


def fleet_report(ws: Workspace, horizon_days: int = 90) -> Doc:
    rows = [r.to_dict() for r in fleet_health(ws)]
    plan = build_plan(ws, horizon_days).to_dict()
    alerts = AlertStore(ws).list("open")
    doc = Doc("Fleet condition report — Tunnel Booster Fans")
    _header(doc, ws, plan["as_of"])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    t = plan["totals"]
    doc.h2("Summary")
    doc.kv([("Fans", len(rows)), ("Health", ", ".join(f"{k} {v}" for k, v in counts.items())),
            ("Open alerts", len(alerts)),
            (f"Next {horizon_days} days — OEM man-hours", t["man_hours_oem"]),
            (f"Next {horizon_days} days — condition-based man-hours", t["man_hours_cbm"]),
            ("Man-hours saved (work-at-height hours saved)", f"{t['man_hours_saved']:g} ({t['height_hours_saved']:g})"),
            ("OEM PM tasks deferred", t["tasks_deferred"])])
    doc.h2("Fleet health (worst first)")
    table_rows = [[r["fan_id"], r["station"], r["capacity_class"],
                   None if r["health_index"] is None else round(r["health_index"]), r["status"],
                   r["top_mode"] if r["top_confidence"] >= 0.5 else "", r["action"], (r["last_run"] or "")[:10]]
                  for r in rows]
    headers = ["Fan", "Station", "Class", "Health index", "Status", "Likely fault", "Action", "Last run"]
    doc.table(headers, table_rows)
    doc.csv_table = (["fan_id", "station", "capacity_class", "health_index", "status", "likely_fault", "action", "last_run"],
                     table_rows)
    doc.h2(f"Plan by station — next {horizon_days} days")
    for st in plan["stations"]:
        urgent = [i for i in st["items"] if i["kind"] != "OEM PM"]
        doc.p(f"{st['station']}: {len(st['items'])} task(s), {st['height_tasks']} at height, {st['man_hours']:g} man-hours"
              + (f", {len(urgent)} condition-driven" if urgent else ""))
    doc.table(["Station", "Due", "Fan", "Type", "Task", "Height"],
              [[st["station"], i["due"], i["fan_id"], i["kind"], i["task"], "yes" if i["needs_work_at_height"] else "no"]
               for st in plan["stations"] for i in st["items"] if i["kind"] != "OEM PM"])
    doc.h2("Deferred OEM PM (condition healthy)")
    doc.table(["Fan", "Station", "Task", "OEM due", "Deferred to", "Work at height"],
              [[d["fan_id"], d["station"], d["task"], d["oem_due"], d["deferred_to"],
                "yes" if d["needs_work_at_height"] else "no"] for d in plan["deferred"]])
    doc.h2("Open alerts")
    doc.table(["ID", "Severity", "Fan", "Title", "First seen"],
              [[a["id"], a["severity"], a["fan_id"], a["title"], ws.settings.fmt(a["created_at"])] for a in alerts])
    doc.p("Assumptions: " + ", ".join(f"{k} {v}" for k, v in plan["assumptions"]["man_hours"].items())
          + " man-hours per occurrence; failures prevented are not counted as savings.")
    doc.data = {"fleet": rows, "plan": plan, "open_alerts": alerts}
    return doc


def render(doc: Doc, fmt: str) -> str:
    if fmt not in FORMATS:
        raise TbfmonError(f"unknown format {fmt!r}", f"use one of {', '.join(FORMATS)}")
    if fmt == "json":
        return json.dumps({"title": doc.title, **doc.data}, indent=2, ensure_ascii=False, default=str)
    return {"md": to_markdown, "html": to_html, "csv": to_csv}[fmt](doc)


def write_report(ws: Workspace, doc: Doc, fmt: str, out: Path | None, stem: str) -> Path | None:
    text = render(doc, fmt)
    if out is not None and str(out) == "-":
        print(text)
        return None
    path = out or ws.home.reports_dir / f"{stem}_{dt.date.today():%Y%m%d}.{fmt}"
    if path.is_dir():
        path = path / f"{stem}_{dt.date.today():%Y%m%d}.{fmt}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
