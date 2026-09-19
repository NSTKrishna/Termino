"""Operator commands: analyze, fleet (health view), explain — and shared renderers.

Rendering only: all logic lives in tbfmon.services (DESIGN.md §12).
"""

from __future__ import annotations

from contextlib import contextmanager
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
        if tr["eta"] is not None:
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


# ---------------------------------------------------------------------- commands


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
