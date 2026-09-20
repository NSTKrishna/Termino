"""Operator commands: analyze, fleet (health view), explain — and shared renderers.

Rendering only: all logic lives in tbfmon.services (DESIGN.md §12).
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import typer
from rich import box
from rich.panel import Panel

from .render import (conf_bar, emit_json, err, fmt, health_label, hi_bar, out, param_label, severity_label,
                     sparkline, table)
from .services.errors import TbfmonError
from .services.workspace import Workspace

ACTION_STYLE = {"no action": "dim", "defer OEM PM": "green", "targeted inspection": "yellow",
                "urgent corrective": "bold red"}


def action_label(action: str) -> str:
    return f"[{ACTION_STYLE.get(action, '')}]{action}[/]"


@contextmanager
def guarded(as_json: bool) -> Iterator[None]:
    """Turn user errors into a friendly message (and JSON), exit code 3, no traceback."""
    try:
        yield
    except TbfmonError as e:
        if as_json:
            emit_json(e.to_dict())
        err.print(f"[bold red]error:[/] {e.message}" + (f"\n[dim]hint:[/] {e.hint}" if e.hint else ""))
        raise typer.Exit(e.code) from None


def open_ws(ctx: typer.Context) -> Workspace:
    return Workspace.open(ctx.obj)


def finish(code: int) -> None:
    if code:
        raise typer.Exit(code)


# ---------------------------------------------------------------------- renderers


def render_analysis(d: dict, ws: Workspace, compact: bool = False) -> None:
    rec = d["recommendation"]
    head = (f"[bold]{d['fan_id']}[/]  {d['station']} · class {d['capacity_class']} · run #{d['run_id']} "
            f"{ws.settings.fmt(d['run_ts'])} ({d['run_type']}, {d['duration_min']:.0f} min)"
            + ("" if d["is_latest_run"] else "  [yellow](not the latest run)[/]") + "\n"
            f"Health  {hi_bar(d['health_index'], d['status'])}  {health_label(d['status'])}")
    if d["anomaly_percentile"] is not None:
        head += f"   anomaly percentile {d['anomaly_percentile']:.2f}"
    out.print(Panel(head, box=box.ROUNDED, expand=False))
    if not compact:
        t = table(None, "parameter", ("measured", {"justify": "right"}), "unit", ("limit (warn / alarm)", {"justify": "right"}),
                  ("deviation %", {"justify": "right"}), "status", box=box.SIMPLE_HEAD, pad_edge=False)
        for p in d["params"]:
            lim = ""
            if p["warn"] is not None or p["alarm"] is not None:
                sign = "≥" if p["side"] == "high" else "≤"
                lim = f"{sign}{fmt(p['warn'])} / {fmt(p['alarm'])}"
            dev = "" if p["deviation_pct"] is None else f"{p['deviation_pct']:+.0f}%"
            if p["deviation_pct"] is not None and p["deviation_pct"] > 0:
                dev = f"[bold]{dev}[/]"
            t.add_row(p["label"], fmt(p["measured"]), p["unit"], lim, dev, param_label(p["status"]))
        out.print(t)
    if d["missing_sensors"]:
        out.print(f"[yellow]missing sensors on this run:[/] {', '.join(d['missing_sensors'])}")
    for n in d["notes"]:
        out.print(f"[dim]note: {n}[/]")
    if d["fault_modes"]:
        out.print("[bold]Likely fault modes[/]")
        for m in d["fault_modes"]:
            loc = f" ({m['location']})" if m["location"] else ""
            out.print(f"  {conf_bar(m['confidence'])}  {m['mode']}{loc} — {m['reasons'][0]}")
    else:
        out.print("[dim]No fault signature detected.[/]")
    body = f"{action_label(rec['action'])}" + (f"  by {rec['by_date']}" if rec["by_date"] else "")
    body += "  · work at height" if rec["needs_work_at_height"] else ""
    for task in rec["tasks"][:5]:
        body += f"\n  • {task}"
    codes = [c for c in rec["reason_codes"] if not c.startswith("LIMITED")]
    if codes:
        body += f"\n[dim]reasons: {', '.join(codes)}[/]"
    out.print(Panel(body, title="Recommended action", box=box.ROUNDED, expand=False))


SHORT_ACTION = {"no action": "—", "defer OEM PM": "defer PM", "targeted inspection": "inspect",
                "urgent corrective": "URGENT"}


def render_fleet(rows: list[dict], ws: Workspace, title: str) -> None:
    wide = out.width >= 120
    nw = {"no_wrap": True}
    n_hist = 12 if wide else 8
    if wide:
        cols = [("fan", nw), ("station", {"no_wrap": True, "max_width": 18, "overflow": "ellipsis"}), ("cls", nw),
                ("health", nw), ("status", nw), ("HI trend", nw), ("vib DE trend", nw),
                ("likely fault", {"max_width": 26, "overflow": "ellipsis", "no_wrap": True}), ("action", nw)]
    else:  # 80-column SSH terminals: fewer, shorter columns instead of truncated ones
        fixed = {"fan": 7, "health": 8, "status": 8, "HI trend": n_hist, "action": 8}
        spare = max(10, out.width - sum(fixed.values()) - 3 * (len(fixed) + 1) - 1)
        cols = [(name, {"no_wrap": True, "min_width": w}) for name, w in list(fixed.items())[:4]]
        cols += [("likely fault", {"no_wrap": True, "overflow": "ellipsis", "max_width": spare}),
                 ("action", {"no_wrap": True, "min_width": 8})]
    t = table(title, *cols, box=box.SIMPLE_HEAD, pad_edge=False)
    for r in rows:
        fault = f"{r['top_mode']} {r['top_confidence'] * 100:.0f}%" if r["top_mode"] and r["top_confidence"] >= 0.3 else ""
        spark = sparkline(r["hi_history"][-n_hist:])
        if wide:
            t.add_row(r["fan_id"], r["station"], r["capacity_class"], hi_bar(r["health_index"], r["status"], 8),
                      health_label(r["status"]), spark, sparkline(r["vib_history"]), fault, action_label(r["action"]))
        else:
            t.add_row(r["fan_id"], hi_bar(r["health_index"], r["status"], 5, compact=True), health_label(r["status"]),
                      spark, fault, f"[{ACTION_STYLE.get(r['action'], '')}]{SHORT_ACTION[r['action']]}[/]")
    out.print(t)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out.print("  ".join(f"{health_label(k)} {v}" for k, v in counts.items())
              + f"   [dim]trends: last {n_hist} runs (· = not measured)[/]")


def render_explain(d: dict, ws: Workspace) -> None:
    h, rec = d["health"], d["recommendation"]
    out.print(Panel(f"[bold]{d['fan_id']}[/] — why: {', '.join(h['reasons']) or 'nothing flagged'}\n"
                    f"Health {hi_bar(h['index'], h['status'])} {health_label(h['status'])} · last run "
                    f"{ws.settings.fmt(d['run_ts'])} → {action_label(rec['action'])}",
                    title="Explanation", box=box.ROUNDED, expand=False))
    if d["limit_exceedances"]:
        for x in d["limit_exceedances"]:
            out.print(f"  limit {param_label(x['status'])}: {x['parameter']} = {fmt(x['value'])} "
                      f"({x['pct_deviation']:+.0f}% vs WARN)")
    if d["contributing_features"]:
        t = table("Contributing features (vs this fan's own baseline and its class peers)", "feature",
                  ("value", {"justify": "right"}), ("baseline", {"justify": "right"}), ("z", {"justify": "right"}),
                  ("peer z", {"justify": "right"}), "evidence from", box=box.SIMPLE_HEAD, pad_edge=False)
        for c in d["contributing_features"][:8]:
            t.add_row(f"{c['label']} [dim]{c['unit']}[/]", fmt(c["value"]), fmt(c["baseline"]),
                      "" if c["z"] is None else f"{c['z']:+.1f}", "" if c["peer_z"] is None else f"{c['peer_z']:+.1f}",
                      ", ".join(c["sources"]))
        out.print(t)
    else:
        out.print("[green]No feature deviates notably from its baseline or peers.[/]")
    rules = d["fault_modes"]["rules"]
    for m in rules[:3]:
        loc = f" ({m['location']})" if m["location"] else ""
        out.print(f"  {conf_bar(m['confidence'])} [bold]{m['mode']}{loc}[/]")
        for r in m["reasons"]:
            out.print(f"      · {r}")
    for tr in d["trends"]:
        if tr["eta"] is not None and tr["eta"] <= 3 * 365:  # longer projections are not actionable
            lo, hi = tr["eta_ci"]
            hi_s = "∞" if hi is None or hi > 1e6 else f"{hi:.0f}"
            out.print(f"  trend: {tr['indicator']} {tr['status']} — ALARM in ~{tr['eta']:.0f} days (CI {lo:.0f}–{hi_s})")
    sim = d["similar_past_failures"]
    if sim:
        t = table("Similar past failures in this fleet (by deviation signature)", ("sim", {"justify": "right"}),
                  "fan", "date", "mode", "shared deviations", "root cause → fix", box=box.SIMPLE_HEAD, pad_edge=False)
        for s in sim:
            t.add_row(f"{s['similarity']:.2f}", s["fan_id"] + (" (same fan)" if s["same_fan"] else ""), s["date"],
                      s["failure_mode"], ", ".join(s["shared_deviations"]), f"{s['root_cause']}\n[dim]→ {s['fix']}[/]")
        out.print(t)
    else:
        out.print("[dim]No recorded failure has a similar deviation signature.[/]")


OUTCOME_STYLE = {"new": "bold", "escalated": "bold", "deduplicated": "dim", "suppressed": "dim"}


def render_outcomes(outs: list[dict], show_quiet: bool = False) -> None:
    """Terminal channel: one line per raised/escalated alert (deduplicated/suppressed summarised)."""
    loud = [o for o in outs if o["outcome"] in ("new", "escalated")]
    quiet = len(outs) - len(loud)
    for o in sorted(loud, key=lambda o: ("INFO", "WARN", "ALARM", "CRITICAL").index(o["severity"]), reverse=True):
        out.print(f"  🔔 {severity_label(o['severity']):>8} #{o['alert_id']} [{OUTCOME_STYLE[o['outcome']]}]"
                  f"{o['outcome']}[/] {o['title']} — {o['message']}")
    if show_quiet:
        for o in outs:
            if o["outcome"] not in ("new", "escalated"):
                out.print(f"  [dim]{o['outcome']}: #{o['alert_id']} {o['title']}[/]")
    elif quiet:
        out.print(f"  [dim]{quiet} repeat alert(s) deduplicated or in cooldown[/]")


def render_alert_list(rows: list[dict], ws: Workspace) -> None:
    t = table(f"Alerts ({len(rows)})", ("id", {"justify": "right"}), "severity", "fan", "title", "first seen",
              ("×", {"justify": "right"}), "status", box=box.SIMPLE_HEAD, pad_edge=False)
    for r in rows:
        st = "open" if r["status"] == "open" else f"acked by {r['acked_by']} {ws.settings.fmt(r['acked_at'])}"
        t.add_row(str(r["id"]), severity_label(r["severity"]), r["fan_id"] or "—", r["title"],
                  ws.settings.fmt(r["created_at"]), str(r["occurrences"]), st)
    out.print(t)


def _deviations(fan: dict) -> str:
    devs = [f"{p['label']} {fmt(p['measured'])} {p['status']}" for p in fan["params"] if p["status"] in ("ALARM", "WARN")]
    sus = [p["label"] for p in fan["params"] if p["status"] == "SUSPECT"]
    txt = ", ".join(devs)
    if sus:
        txt += ("; " if txt else "") + f"[magenta]re-measure: {', '.join(sus)}[/]"
    return txt or "[green]within limits[/]"


def render_fan_rows(fans: list[dict], title: str, file_col: bool = False) -> None:
    cols = [("#", {"justify": "right"}), "fan", "health", "status", "deviations from specified values",
            "likely fault", "action"]
    if file_col:
        cols.append("file")
    t = table(title, *cols, box=box.SIMPLE_HEAD, pad_edge=False)
    for k, f in enumerate(fans, 1):
        mode = next((f"{m['mode']} {m['confidence'] * 100:.0f}%" for m in f["fault_modes"] if m["confidence"] >= 0.5), "")
        miss = f"\n[dim]missing: {len(f['missing_sensors'])} sensor group(s)[/]" if f["missing_sensors"] else ""
        row = [str(k), f["fan_id"], hi_bar(f["health_index"], f["status"], 6, compact=True), health_label(f["status"]),
               _deviations(f) + miss, mode, action_label(f["recommendation"]["action"])]
        if file_col:
            row.append(f.get("_file", ""))
        t.add_row(*row)
    out.print(t)


def render_drill(res: dict, ws: Workspace) -> None:
    ing = res["ingest"]["totals"]
    out.print(Panel(f"[bold]Mock drill — {res['station']}[/]  ·  file {Path(res['file']).name}\n"
                    f"ingested {ing['inserted']} new, {ing['updated']} updated, {ing['unchanged']} unchanged, "
                    f"{ing['rejected']} rejected  ·  {len(res['fans'])} fan(s) analysed",
                    box=box.ROUNDED, expand=False))
    render_fan_rows(res["fans"], "Risk-sorted summary (highest risk first)")
    for s in res["skipped"][:5]:
        out.print(f"  [yellow]skipped[/] {s['row']}: {s['reason']}")
    if res["unknown_columns"]:
        out.print(f"  [yellow]ignored unknown columns:[/] {', '.join(res['unknown_columns'])}")
    for n in res["notes"]:
        out.print(f"  [dim]note: {n}[/]")
    if res["alerts"]:
        out.print("[bold]Alerts[/]")
        render_outcomes(res["alerts"])
    else:
        out.print("[green]No deviations — no alerts raised.[/]")


def render_plan(pl: dict, ws: Workspace) -> None:
    t = pl["totals"]
    out.print(Panel(f"[bold]Condition-based maintenance plan[/] · {pl['as_of']} → {pl['end']} ({pl['horizon_days']} days)\n"
                    + "  ".join(f"{action_label(k)} {v}" for k, v in t["actions"].items()),
                    box=box.ROUNDED, expand=False))
    for st in pl["stations"]:
        t_st = table(f"{st['station']} — {len(st['items'])} task(s), {st['height_tasks']} at height, "
                     f"{st['man_hours']:g} man-h", "due", "fan", "type", "task", "height", ("man-h", {"justify": "right"}),
                     box=box.SIMPLE_HEAD, pad_edge=False)
        for i in st["items"]:
            kind = action_label(i["kind"]) if i["kind"] != "OEM PM" else "OEM PM"
            t_st.add_row(i["due"], i["fan_id"], kind, i["task"],
                         "[bold yellow]⇡ HEIGHT[/]" if i["needs_work_at_height"] else "ground", f"{i['man_hours']:g}")
        out.print(t_st)
    if pl["deferred"]:
        d = table(f"Deferred OEM PMs ({len(pl['deferred'])}) — condition healthy, related faults not indicated",
                  "fan", "station", "task", "OEM due", "deferred to", "height", box=box.SIMPLE_HEAD, pad_edge=False)
        for x in pl["deferred"]:
            d.add_row(x["fan_id"], x["station"], x["task"], x["oem_due"] or "—", x["deferred_to"],
                      "⇡" if x["needs_work_at_height"] else "")
        out.print(d)
    s = table(f"Man-hours, next {pl['horizon_days']} days: time-based vs condition-based", "",
              ("OEM schedule", {"justify": "right"}), ("CBM plan", {"justify": "right"}), ("saved", {"justify": "right"}))
    s.add_row("PM task occurrences", str(t["oem_tasks"]), str(t["cbm_tasks"]), str(t["oem_tasks"] - t["cbm_tasks"]))
    s.add_row("man-hours", f"{t['man_hours_oem']:g}", f"{t['man_hours_cbm']:g}", f"[bold green]{t['man_hours_saved']:g}[/]")
    s.add_row("work-at-height hours", f"{t['height_hours_oem']:g}", f"{t['height_hours_cbm']:g}",
              f"[bold green]{t['height_hours_saved']:g}[/]")
    out.print(s)
    mh = pl["assumptions"]["man_hours"]
    out.print(f"[dim]assumptions (engine.yaml): {mh['at_height']:g} h per at-height task, {mh['ground']:g} h ground, "
              f"{mh['targeted_inspection']:g} h inspection, {mh['urgent_corrective']:g} h corrective; never deferred: "
              f"{pl['assumptions']['never_deferred']}; failures prevented not counted.[/]")
    for n in pl["notes"]:
        out.print(f"[dim]note: {n}[/]")


# ---------------------------------------------------------------------- commands


def _print_event(d: dict, ws: Workspace) -> None:
    """Line-oriented output when not attached to a terminal (logs, cron, pipes)."""
    if not d["ok"]:
        out.print(f"[{ws.settings.fmt(d['at'])}] {d['file']}: [red]error[/] {d['error']}"
                  + (f" ({d['hint']})" if d["hint"] else ""))
        return
    worst = d["fans"][0] if d["fans"] else None
    out.print(f"[{ws.settings.fmt(d['at'])}] {d['file']}: {len(d['fans'])} fan(s)"
              + (f", worst {worst['fan_id']} {health_label(worst['status'])} {worst['health_index']:.0f}" if worst else ""))
    render_outcomes(d["alerts"])


def _watch_view(state: dict, directory: Path, every: float, ws: Workspace):
    from rich.console import Group

    fans = sorted(state["fans"].values(), key=lambda f: ({"Critical": 0, "Degraded": 1, "Watch": 2, "Healthy": 3}[f["status"]],
                                                         f["health_index"]))
    head = Panel(f"watching [bold]{directory}[/] every {every:g}s · polls {state['polls']} · files "
                 f"{len(state['events'])} · alerts {len(state['alerts'])} · {ws.settings.fmt(ws.settings.now())}  "
                 "[dim](Ctrl-C to stop)[/]", box=box.ROUNDED)
    t = table("Latest assessment per fan", "fan", "health", "status", "deviations", "action", "file",
              box=box.SIMPLE_HEAD, pad_edge=False)
    for f in fans[:15]:
        t.add_row(f["fan_id"], hi_bar(f["health_index"], f["status"], 6, compact=True), health_label(f["status"]),
                  _deviations(f), action_label(f["recommendation"]["action"]), f["_file"])
    feed = table("Alert feed (newest first)", "severity", "alert", box=box.SIMPLE_HEAD, pad_edge=False)
    for a in list(reversed(state["alerts"]))[:8]:
        feed.add_row(severity_label(a["severity"]), f"#{a['alert_id']} {a['title']} — {a['message']}")
    errors = [e for e in state["events"] if not e["ok"]][-3:]
    parts = [head, t, feed] + [f"[red]error[/] {e['file']}: {e['error']}" for e in errors]
    return Group(*parts)


def register(app: typer.Typer, fleet_app: typer.Typer) -> None:
    from .cli import AsOfOpt, JsonOpt, _placeholder_banner
    from .services.analysis import analyze_fan, fleet_exit, fleet_health
    from .services.explain import explain_fan

    @app.command()
    def analyze(
        ctx: typer.Context,
        fan: str = typer.Option(..., "--fan", help="Fan ID, e.g. TBF-012 (or just 12)."),
        run: Optional[int] = typer.Option(None, "--run", help="Run ID to analyse (see --json of `fan show`)."),
        latest: bool = typer.Option(False, "--latest", help="Analyse the fan's latest run (default)."),
        as_json: bool = JsonOpt,
    ) -> None:
        """Readings vs limits, health index, likely fault modes and action for one run. Exit 0/1/2."""
        with guarded(as_json):
            if run is not None and latest:
                raise TbfmonError("use either --run or --latest, not both")
            ws = open_ws(ctx)
            res = analyze_fan(ws, fan, run).to_dict()
        if as_json:
            emit_json(res)
        else:
            render_analysis(res, ws)
            _placeholder_banner(ws.home)
        finish(res["exit_code"])

    @fleet_app.callback(invoke_without_command=True)
    def fleet(
        ctx: typer.Context,
        station: Optional[str] = typer.Option(None, "--station", help="Filter by station."),
        capacity: Optional[str] = typer.Option(None, "--capacity", "--class", help="Filter by capacity class, e.g. C45."),
        status: Optional[str] = typer.Option(None, "--status", help="healthy | watch | degraded | critical."),
        as_of: Optional[str] = AsOfOpt,
        as_json: bool = JsonOpt,
    ) -> None:
        """Fleet health table with sparklines (`fleet list` shows the limit-condition view). Exit 0/1/2."""
        if ctx.invoked_subcommand:
            return
        with guarded(as_json):
            ws = open_ws(ctx)
            rows = fleet_health(ws, station, capacity, status, as_of=as_of)
            code = fleet_exit(rows)
            data = [r.to_dict() for r in rows]
            as_of_d = ws.assessment(as_of).data.as_of
        if as_json:
            emit_json({"as_of": as_of_d.isoformat(), "fans": data})
        else:
            render_fleet(data, ws, f"Fleet health — {len(rows)} fans (as of {as_of_d}) · worst first")
            _placeholder_banner(ws.home)
        finish(code)

    alerts_app = typer.Typer(help="Persisted alerts: list and acknowledge.", no_args_is_help=True)
    app.add_typer(alerts_app, name="alerts")

    @alerts_app.command("list")
    def alerts_list(
        ctx: typer.Context,
        status: str = typer.Option("open", "--status", help="open | acked | all."),
        fan: Optional[str] = typer.Option(None, "--fan", help="Only this fan."),
        severity: Optional[str] = typer.Option(None, "--severity", help="Minimum severity: INFO|WARN|ALARM|CRITICAL."),
        as_json: bool = JsonOpt,
    ) -> None:
        """List alerts, most severe first. Exit code = worst listed severity (0/1/2)."""
        from .services.alerts import AlertStore, alerts_exit

        with guarded(as_json):
            ws = open_ws(ctx)
            rows = AlertStore(ws).list(status, fan, severity)
        if as_json:
            emit_json({"alerts": rows})
        elif rows:
            render_alert_list(rows, ws)
        else:
            out.print(f"[green]No {status if status != 'all' else ''} alerts.[/]")
        finish(alerts_exit(rows) if status != "acked" else 0)

    @alerts_app.command("ack")
    def alerts_ack(
        ctx: typer.Context,
        ids: Optional[list[int]] = typer.Argument(None, help="Alert IDs to acknowledge."),
        all_open: bool = typer.Option(False, "--all", help="Acknowledge every open alert (optionally --fan)."),
        fan: Optional[str] = typer.Option(None, "--fan", help="With --all: only this fan."),
        user: Optional[str] = typer.Option(None, "--user", help="Who acknowledges (default: config/TBFMON_USER/OS user)."),
        note: Optional[str] = typer.Option(None, "--note", help="Free-text note stored with the ack."),
        as_json: bool = JsonOpt,
    ) -> None:
        """Acknowledge alerts (recorded with user and IST timestamp; starts the cooldown)."""
        from .services.alerts import AlertStore

        with guarded(as_json):
            ws = open_ws(ctx)
            rows = AlertStore(ws).ack(list(ids or []), user, note, all_open, fan)
        if as_json:
            emit_json({"acknowledged": rows})
            return
        for r in rows:
            out.print(f"  #{r['id']} {severity_label(r['severity'])} {r['title']}: {r['result']}"
                      + (f" by {r['acked_by']} at {ws.settings.fmt(r['acked_at'])}" if r["acked_at"] else ""))

    @app.command()
    def drill(
        ctx: typer.Context,
        station: str = typer.Option(..., "--station", help="Station name (case-insensitive, partial OK)."),
        file: Optional[Path] = typer.Option(None, "--file", help="CSV of drill readings (test_runs columns)."),
        simulate: bool = typer.Option(False, "--simulate", help="Generate realistic drill readings for the station."),
        inject: Optional[list[str]] = typer.Option(None, "--inject", help="With --simulate: FAN:MODE fault to inject (repeatable)."),
        seed: Optional[int] = typer.Option(None, "--seed", help="With --simulate: random seed."),
        as_json: bool = JsonOpt,
    ) -> None:
        """Mock-drill mode: ingest, analyse every fan, risk-sorted summary, alert on deviations. Exit 0/1/2."""
        from .services.drill import run_drill

        with guarded(as_json):
            ws = open_ws(ctx)
            res = run_drill(ws, station, file, simulate, inject, seed).to_dict()
        if as_json:
            emit_json(res)
        else:
            render_drill(res, ws)
        finish(res["exit_code"])

    @app.command()
    def watch(
        ctx: typer.Context,
        directory: Path = typer.Argument(..., help="Folder where reading CSVs land."),
        interval: Optional[float] = typer.Option(None, "--interval", help="Polling interval in seconds (config: watch_interval_s)."),
        once: bool = typer.Option(False, "--once", help="Process files already present, then exit (cron)."),
        duration: Optional[float] = typer.Option(None, "--duration", help="Stop after this many seconds."),
        simulate_feed: bool = typer.Option(False, "--simulate-feed", help="Write simulated drill files into the folder (demo)."),
        feed_count: int = typer.Option(4, "--feed-count", min=1, help="Files the simulated feed writes."),
        feed_every: float = typer.Option(3.0, "--feed-every", min=0.1, help="Seconds between simulated files."),
        as_json: bool = JsonOpt,
    ) -> None:
        """Watch a folder and alert in real time as reading files arrive. Exit = worst severity seen."""
        from .services.watch import DirectoryWatcher, FeedSimulator

        with guarded(as_json):
            ws = open_ws(ctx)
            directory = directory.expanduser()
            if simulate_feed:
                directory.mkdir(parents=True, exist_ok=True)
            watcher = DirectoryWatcher(ws, directory)
            every = interval or ws.settings.watch_interval_s
        feed = None
        if simulate_feed:
            home = ctx.obj
            feed = FeedSimulator(lambda: Workspace.open(home), directory, feed_count, feed_every)
            feed.start()
        state = {"events": [], "fans": {}, "alerts": [], "polls": 0, "worst": 0}

        def handle(ev) -> None:
            d = ev.to_dict()
            state["events"].append(d)
            state["worst"] = max(state["worst"], d["exit_code"])
            for f in d["fans"]:
                state["fans"][f["fan_id"]] = {**f, "_file": d["file"]}
            state["alerts"].extend(a for a in d["alerts"] if a["outcome"] in ("new", "escalated"))
            if as_json:
                print(json.dumps({"event": "file", **d}, ensure_ascii=False, default=str), flush=True)
            elif not live_mode:
                _print_event(d, ws)

        live_mode = out.is_terminal and not as_json and not once
        start = time.monotonic()

        def done() -> bool:
            if duration is not None and time.monotonic() - start >= duration:
                return True
            if feed is not None and duration is None:
                return not feed.is_alive() and state["polls"] > 0 and not watcher.ready_peek()
            return False

        def loop(live=None) -> None:
            watcher.prime()
            if once:
                for ev in watcher.poll():
                    handle(ev)
                return
            while not done():
                time.sleep(every)
                for ev in watcher.poll():
                    handle(ev)
                state["polls"] += 1
                if live is not None:
                    live.update(_watch_view(state, directory, every, ws))

        try:
            with guarded(as_json):
                if live_mode:
                    from rich.live import Live
                    with Live(_watch_view(state, directory, every, ws), console=out, refresh_per_second=4) as live:
                        loop(live)
                else:
                    if not as_json:
                        out.print(f"watching {directory} every {every:g}s (Ctrl-C to stop)")
                    loop()
        except KeyboardInterrupt:
            pass
        finally:
            if feed is not None:
                feed.stop_event.set()
        if feed is not None and feed.error:
            err.print(f"[yellow]simulated feed stopped: {feed.error}[/]")
        if as_json:
            print(json.dumps({"event": "summary", "files": len(state["events"]), "alerts": len(state["alerts"]),
                              "exit_code": state["worst"]}), flush=True)
        else:
            out.print(f"processed {len(state['events'])} file(s), raised {len(state['alerts'])} alert(s)")
        finish(state["worst"])

    @app.command()
    def plan(
        ctx: typer.Context,
        horizon: str = typer.Option("90d", "--horizon", help="Planning horizon: 90d, 12w, 6m, 1y."),
        station: Optional[str] = typer.Option(None, "--station", help="Only this station."),
        as_of: Optional[str] = AsOfOpt,
        as_json: bool = JsonOpt,
    ) -> None:
        """Condition-based maintenance plan by station vs the OEM schedule. Exit 2 urgent · 1 inspections · 0."""
        from .services.plan import build_plan, parse_horizon

        with guarded(as_json):
            ws = open_ws(ctx)
            pl = build_plan(ws, parse_horizon(horizon), as_of, station).to_dict()
        if as_json:
            emit_json(pl)
        else:
            render_plan(pl, ws)
            _placeholder_banner(ws.home)
        finish(pl["exit_code"])

    @app.command()
    def report(
        ctx: typer.Context,
        fan: Optional[str] = typer.Option(None, "--fan", help="Report for one fan."),
        fleet_: bool = typer.Option(False, "--fleet", help="Report for the whole fleet."),
        fmt_: str = typer.Option("md", "--format", help="md | html | csv | json."),
        out_path: Optional[Path] = typer.Option(None, "--out", help="File or folder (default: <home>/reports/); '-' = stdout."),
        horizon: str = typer.Option("90d", "--horizon", help="Plan horizon in fleet reports."),
        as_json: bool = JsonOpt,
    ) -> None:
        """Write a report file (Markdown, static HTML, CSV or JSON). HTML is a file, never a server."""
        from .services.plan import parse_horizon
        from .services.reports import FORMATS, fan_report, fleet_report, write_report

        with guarded(as_json):
            if bool(fan) == bool(fleet_):
                raise TbfmonError("give exactly one of --fan ID or --fleet")
            if fmt_.lower() not in FORMATS:
                raise TbfmonError(f"unknown format {fmt_!r}", f"use one of {', '.join(FORMATS)}")
            ws = open_ws(ctx)
            doc = fan_report(ws, fan) if fan else fleet_report(ws, parse_horizon(horizon))
            stem = f"fan_{doc.data['analysis']['fan_id']}" if fan else "fleet"
            path = write_report(ws, doc, fmt_.lower(), out_path, stem)
        if path is None:
            return
        if as_json:
            emit_json({"report": str(path), "format": fmt_.lower(), "title": doc.title})
        else:
            out.print(f"[green]✓[/] {doc.title} → {path}", soft_wrap=True)

    @app.command()
    def explain(
        ctx: typer.Context,
        fan: str = typer.Option(..., "--fan", help="Fan ID."),
        as_of: Optional[str] = AsOfOpt,
        as_json: bool = JsonOpt,
    ) -> None:
        """Why a fan was flagged: contributing features, rules and similar past failures. Exit 0/1/2."""
        with guarded(as_json):
            ws = open_ws(ctx)
            d = explain_fan(ws, fan, as_of)
        if as_json:
            emit_json(d)
        else:
            render_explain(d, ws)
        finish(d["exit_code"])
