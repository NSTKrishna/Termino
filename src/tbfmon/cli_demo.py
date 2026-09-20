"""`tbfmon demo` — a scripted 2–3 minute story using the same services as the real commands.

simulate → ingest → fleet → mock drill with two deviating fans → alerts → explain → plan → savings.
Runs in its own folder (default ./tbfmon_demo, marked so it can be safely recreated).
"""

from __future__ import annotations

import datetime as dt
import shutil
import time
from pathlib import Path

import typer
from rich import box
from rich.panel import Panel
from rich.rule import Rule

from .cli_ops import (guarded, render_alert_list, render_drill, render_explain, render_fleet, render_plan)
from .paths import Home
from .render import out, table
from .services.errors import TbfmonError

MARKER = ".tbfmon-demo"
STEPS = 8
PAUSE_S = 18.0  # 7 pauses + ~10 s of work ≈ 2–3 minutes


def _prepare_dir(path: Path) -> Home:
    if path.exists() and any(path.iterdir()):
        if not (path / MARKER).exists():
            raise TbfmonError(f"{path} exists and is not a demo folder",
                              "remove it or choose another folder with --dir")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / MARKER).write_text("created by `tbfmon demo`; safe to delete\n")
    return Home(path.resolve())


def register(app: typer.Typer) -> None:
    @app.command()
    def demo(
        fast: bool = typer.Option(False, "--fast", help="Skip the pauses between steps."),
        directory: Path = typer.Option(Path("tbfmon_demo"), "--dir", help="Demo workspace (recreated each run)."),
        fans: int = typer.Option(40, "--fans", min=8, max=112, help="Fleet size."),
        pause: float = typer.Option(PAUSE_S, "--pause", min=0, help="Seconds between steps (ignored with --fast)."),
    ) -> None:
        """Scripted 2–3 minute story: simulate → drill with 2 deviating fans → alerts → explain → plan → savings."""
        from .ingest import ingest
        from .services.alerts import AlertStore
        from .services.analysis import fleet_health
        from .services.drill import run_drill
        from .services.explain import explain_fan
        from .services.plan import build_plan
        from .services.setup import init_workspace, simulate_into
        from .services.workspace import Workspace
        from .sources import CsvSource

        t0 = time.monotonic()

        def step(k: int, title: str, blurb: str) -> None:
            if k > 1 and not fast and pause:
                with out.status(f"[dim]next: {title}…[/]", spinner="dots"):
                    time.sleep(pause)
            out.print()
            out.print(Rule(f"[bold]Step {k}/{STEPS} · {title}[/]", align="left"))
            out.print(f"[dim]{blurb}[/]")

        with guarded(False):
            out.print(Panel("[bold]tbfmon demo[/] — condition-based maintenance for Tunnel Booster Fans\n"
                            "Terminal only · all data below is [bold yellow]SIMULATED[/] · limits are placeholders",
                            box=box.DOUBLE, expand=False))

            step(1, "Simulate a fleet", "No real DMRC data exists, so we generate 3 years of sparse test-run history "
                 "with realistic degradation, failures, OEM PM records and instrument glitches.")
            home = _prepare_dir(directory)
            init_workspace(home)
            ds, files, rep = simulate_into(home, fans, 3, 42, dt.date(2023, 1, 1))
            s = ds.summary()
            ws = Workspace.open(home)
            pm = ds.maintenance[ds.maintenance["type"] == "preventive"]
            fy = fans * 3
            t = table(None, "", ("value", {"justify": "right"}), box=box.SIMPLE, pad_edge=False)
            for k, v in (("fans / stations", f"{s['fans']} / {len(ws.stations())}"), ("test runs", s["test_runs"]),
                         ("runs per fan per year", f"{s['test_runs'] / fy:.1f}"),
                         ("recorded failures", s["failures"]),
                         ("OEM PM visits per fan per year", f"{len(pm) / fy:.1f}"),
                         ("…of which need work at height", f"{pm['work_at_height'].mean() * 100:.0f}%")):
                t.add_row(k, str(v))
            out.print(t)
            out.print(f"workspace: {home.root}", soft_wrap=True)

            step(2, "Ingest", "Real sites deliver CSV exports. Re-ingesting the same files changes nothing (idempotent).")
            r2 = ingest(ws.conn, CsvSource(home.sim_dir))
            tot = r2.to_dict()["totals"]
            out.print(f"CSV bundle re-ingested: {tot['inserted']} new · {tot['updated']} updated · "
                      f"{tot['unchanged']} unchanged · {tot['rejected']} rejected (transcription typos)")
            out.print(f"{r2.values_nulled} impossible values (sentinels, out of range) were nulled and logged.")
            ws.invalidate()

            step(3, "Fleet before the drill", "Health index per fan from limits, drift vs its own baseline, peers, "
                 "anomaly model, fault signatures and trends.")
            rows = [r.to_dict() for r in fleet_health(ws)]
            render_fleet(rows[:6], ws, f"Fleet health — worst 6 of {len(rows)}")

            stations: dict[str, list[dict]] = {}
            for r in rows:
                stations.setdefault(r["station"], []).append(r)
            candidates = [(sum(x["status"] == "Healthy" for x in v), k) for k, v in stations.items() if len(v) >= 4]
            station = sorted(candidates, key=lambda c: (-c[0], c[1]))[0][1] if candidates else rows[0]["station"]
            healthy = sorted(x["fan_id"] for x in stations[station] if x["status"] == "Healthy")
            targets = (healthy + sorted(x["fan_id"] for x in stations[station]))[:2]
            inject = [f"{targets[0]}:bearing_wear", f"{targets[1]}:insulation_degradation"]

            step(4, f"Mock drill at {station}", f"All {len(stations[station])} fans run. Unknown to the operator, "
                 f"{targets[0]} has a worn bearing and {targets[1]} has failing winding insulation.")
            res = run_drill(ws, station, simulate=True, inject=inject, seed=5).to_dict()
            render_drill(res, ws)

            step(5, "Alerts", "Persisted in SQLite with dedupe and cooldown; logged to JSONL; email/SMS/webhook "
                 "stubs in dry-run. Engineers acknowledge with `tbfmon alerts ack ID`.")
            open_alerts = AlertStore(ws).list("open", min_severity="WARN")
            render_alert_list(open_alerts[:8], ws)
            out.print(f"[dim]log: {home.root / ws.settings.alerts.jsonl_log}[/]", soft_wrap=True)

            worst = res["fans"][0]["fan_id"]
            step(6, f"Explain {worst}", "Why it was flagged: which features moved, against which baseline, "
                 "and which past failures looked the same.")
            render_explain(explain_fan(ws, worst), ws)

            step(7, f"Plan the next 90 days at {station}", "Condition-driven work first; related OEM tasks are done "
                 "in the same visit; OEM tasks on healthy fans are deferred; work at height is flagged. At a station "
                 "with two real faults the plan can cost more than the OEM schedule — savings come fleet-wide.")
            render_plan(build_plan(ws, 90, station=station).to_dict(), ws)

            step(8, "What it saves", "Fleet-wide: OEM time-based schedule vs the condition-based plan.")
            p90 = build_plan(ws, 90).to_dict()["totals"]
            p365 = build_plan(ws, 365).to_dict()["totals"]
            t = table(None, "", ("OEM 90 d", {"justify": "right"}), ("CBM 90 d", {"justify": "right"}),
                      ("OEM 1 y", {"justify": "right"}), ("CBM 1 y", {"justify": "right"}), box=box.SIMPLE_HEAD,
                      pad_edge=False)
            t.add_row("PM task occurrences", str(p90["oem_tasks"]), str(p90["cbm_tasks"]), str(p365["oem_tasks"]),
                      str(p365["cbm_tasks"]))
            t.add_row("man-hours", f"{p90['man_hours_oem']:g}", f"{p90['man_hours_cbm']:g}",
                      f"{p365['man_hours_oem']:g}", f"{p365['man_hours_cbm']:g}")
            t.add_row("work-at-height hours", f"{p90['height_hours_oem']:g}", f"{p90['height_hours_cbm']:g}",
                      f"{p365['height_hours_oem']:g}", f"{p365['height_hours_cbm']:g}")
            out.print(t)
            caught = [f for f in res["fans"] if f["fan_id"] in targets and f["status"] in ("Critical", "Degraded")]
            out.print(Panel(
                f"• The drill caught {len(caught)}/2 developing faults "
                f"({', '.join(f['fan_id'] + ': ' + (f['fault_modes'][0]['mode'] if f['fault_modes'] else '?') for f in caught)})"
                f" and raised {sum(a['outcome'] == 'new' for a in res['alerts'])} alerts.\n"
                f"• Next year: ~{p365['man_hours_saved']:g} man-hours and {p365['height_hours_saved']:g} work-at-height "
                f"hours avoided ({p365['tasks_deferred']} OEM tasks deferred on healthy fans).\n"
                "• Estimates use engine.yaml man-hour assumptions and SIMULATED data; failures prevented are not counted.\n"
                f"[dim]Explore: tbfmon --home {home.root} fleet · analyze --fan {worst} · alerts list · "
                "report --fleet --format html · evaluate[/]",
                title="Summary", box=box.ROUNDED, expand=False))
            out.print(f"[dim]demo finished in {time.monotonic() - t0:.0f}s[/]")
