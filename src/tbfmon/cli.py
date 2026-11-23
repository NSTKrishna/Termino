"""`tbfmon` command-line interface. Terminal only — see DESIGN.md §0 and §6."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.panel import Panel

from . import __version__, db
from .ingest import IngestReport, ingest as run_ingest
from .limits import default_limits_text, load_limits
from .paths import Home
from .pm import resolve_as_of
from .render import emit_json, err, fmt, out, sparkline, styled, table
from .sources import CsvSource, GroundTruthRefused, SimulatorSource
from .validate import checks_to_dict, run_checks

# Exit codes: 0 ok · 1 warn · 2 alarm · 3 error (bad input, unknown fan, not initialised).
# Click's usage errors default to 2, which a cron script would read as ALARM. Typer ≥ 0.2x
# vendors Click as typer._click; patch whichever implementation is present.
def _usage_errors_exit_3() -> None:
    import importlib

    for name in ("typer._click.exceptions", "click.exceptions"):
        try:
            importlib.import_module(name).UsageError.exit_code = 3
        except ImportError:
            pass


_usage_errors_exit_3()

app = typer.Typer(
    help="Termino Lite: data and limit monitoring for Tunnel Booster Fans (terminal only).",
    no_args_is_help=True, add_completion=False, rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)
fleet_app = typer.Typer(help="Fleet-wide views.", no_args_is_help=True)
fan_app = typer.Typer(help="Single-fan views.", no_args_is_help=True)
app.add_typer(fleet_app, name="fleet")
app.add_typer(fan_app, name="fan")

JsonOpt = typer.Option(False, "--json", help="Machine-readable JSON on stdout.")
AsOfOpt = typer.Option(None, "--as-of", help="Reference date: YYYY-MM-DD, 'today', or 'data' (default: latest data date).")


def _home(ctx: typer.Context) -> Home:
    return ctx.obj


def _fail(msg: str, as_json: bool, code: int = 3) -> None:
    if as_json:
        emit_json({"error": msg})
    err.print(f"[bold red]error:[/] {msg}")
    raise typer.Exit(code)


def _connect(home: Home, as_json: bool):
    try:
        return db.connect(home.db_path)
    except db.NotInitialized as e:
        _fail(str(e), as_json)


def _placeholder_banner(home: Home) -> None:
    n = load_limits(home.limits_path).placeholder_count()
    if n:
        err.print(f"[yellow]⚠ limits.yaml: {n} placeholder limit(s) — REPLACE WITH OEM VALUES[/]")


CONDITION_EXIT = {"OK": 0, "NA": 0, "WARN": 1, "ALARM": 2}


def _exit(code: int) -> None:
    """Exit with the ok/warn/alarm contract (0/1/2) without printing anything."""
    if code:
        raise typer.Exit(code)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"tbfmon {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    home: Optional[Path] = typer.Option(None, "--home", envvar="TBFMON_HOME",
                                        help="Workspace directory (default ./tbfmon_data)."),
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show version."),
) -> None:
    ctx.obj = Home.resolve(home)


# --------------------------------------------------------------------------- init


@app.command()
def init(ctx: typer.Context, force: bool = typer.Option(False, "--force", help="Overwrite limits.yaml with the defaults."),
         as_json: bool = JsonOpt) -> None:
    """Create the workspace, SQLite schema and an editable limits.yaml."""
    from .services.setup import init_workspace

    home = _home(ctx)
    result = init_workspace(home, force)
    if as_json:
        emit_json(result)
        return
    out.print(f"[green]✓[/] workspace  {home.root}", soft_wrap=True)
    out.print(f"[green]✓[/] database   {home.db_path} (schema v{db.SCHEMA_VERSION})", soft_wrap=True)
    out.print(f"[green]✓[/] limits     {home.limits_path} "
              + ("(written from defaults)" if result["limits_written"] else "(kept existing; --force to reset)"),
              soft_wrap=True)
    _placeholder_banner(home)


# --------------------------------------------------------------------------- simulate


@app.command()
def simulate(
    ctx: typer.Context,
    fans: int = typer.Option(40, "--fans", min=1, max=112, help="Number of fans."),
    years: int = typer.Option(3, "--years", min=1, max=20, help="Years of history."),
    seed: int = typer.Option(42, "--seed", help="Random seed (same seed ⇒ identical files)."),
    start: str = typer.Option("2023-01-01", "--start", help="First day of simulated history."),
    no_ingest: bool = typer.Option(False, "--no-ingest", help="Only write CSVs; don't load the DB."),
    as_json: bool = JsonOpt,
) -> None:
    """Generate a synthetic fleet: CSV bundle + separate ground truth, then ingest."""
    from .services.setup import simulate_into

    home = _home(ctx)
    if not no_ingest:
        _connect(home, as_json)  # friendly "run tbfmon init" before generating anything
    try:
        start_d = dt.date.fromisoformat(start)
    except ValueError:
        _fail(f"--start must be YYYY-MM-DD, got {start!r}", as_json)
    ds, files, report = simulate_into(home, fans, years, seed, start_d, ingest_data=not no_ingest)
    result = {"summary": ds.summary(), "files": {k: str(v) for k, v in files.items()},
              "ingest": report.to_dict() if report else None}
    if as_json:
        emit_json(result)
        return
    s = ds.summary()
    t = table("Simulated dataset", "item", ("value", {"justify": "right"}))
    for k in ("seed", "start", "end", "fans", "test_runs", "maintenance_records", "failures", "fault_episodes"):
        t.add_row(k, str(s[k]))
    t.add_row("episode outcomes", ", ".join(f"{k}={v}" for k, v in s["episode_outcomes"].items()))
    out.print(t)
    out.print(f"CSV bundle   → {home.sim_dir}", soft_wrap=True)
    out.print(f"ground truth → {files['ground_truth']}  [dim](kept separate; never ingested)[/]", soft_wrap=True)
    if report:
        _print_ingest(report)


# --------------------------------------------------------------------------- ingest


def _print_ingest(report: IngestReport) -> None:
    t = table(f"Ingest: {report.source}", "kind", *[(c, {"justify": "right"})
                                                     for c in ("inserted", "updated", "unchanged", "rejected")])
    for kind, c in report.by_kind.items():
        t.add_row(kind, *(str(c.get(o, 0)) for o in ("inserted", "updated", "unchanged", "rejected")))
    t.add_row("[bold]total[/]", *(f"[bold]{report.total(o)}[/]" for o in ("inserted", "updated", "unchanged", "rejected")))
    out.print(t)
    out.print(f"values nulled (sentinel / out of sensor range / unparseable): {report.values_nulled}")
    if report.ignored_columns:
        out.print(f"[yellow]ignored unknown columns:[/] {report.ignored_columns}")
    for ex in report.rejected_examples[:5]:
        out.print(f"  [red]rejected[/] {ex['origin']}: {ex['field']} — {ex['issue']}")


@app.command()
def ingest(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="CSV bundle directory or a single CSV file."),
    kind: Optional[str] = typer.Option(None, "--kind",
                                       help="Record kind for a single file: fan|pm_task|test_run|maintenance|failure."),
    as_json: bool = JsonOpt,
) -> None:
    """Ingest CSV data (idempotent: re-ingesting the same data changes nothing)."""
    home = _home(ctx)
    conn = _connect(home, as_json)
    try:
        source = CsvSource(path, kind=kind)  # type: ignore[arg-type]
    except GroundTruthRefused as e:
        _fail(f"{e} — ground truth is kept separate and is never ingested (DESIGN.md §0)", as_json)
    except (FileNotFoundError, ValueError) as e:
        _fail(str(e), as_json)
    report = run_ingest(conn, source)
    if as_json:
        emit_json(report.to_dict())
    else:
        _print_ingest(report)


# --------------------------------------------------------------------------- validate


@app.command()
def validate(
    ctx: typer.Context,
    examples: int = typer.Option(5, "--examples", min=0, help="Examples to show per failing check."),
    as_of: Optional[str] = AsOfOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Check limits, consistency, suspect values and missingness. Exit 0 PASS · 1 WARN · 2 FAIL."""
    home = _home(ctx)
    conn = _connect(home, as_json)
    ref, _ = resolve_as_of(conn, as_of)
    checks, extra = run_checks(conn, home.limits_path, ref, max_examples=max(examples, 0) if not as_json else 50)
    worst_status = "FAIL" if any(c.status == "FAIL" for c in checks) else (
        "WARN" if any(c.status == "WARN" for c in checks) else "PASS")
    if as_json:
        emit_json({"status": worst_status, "checks": checks_to_dict(checks), **extra})
    else:
        t = table(f"Validation (as of {extra['as_of']})", "check", "status", ("count", {"justify": "right"}), "detail")
        for c in checks:
            t.add_row(c.name, styled(c.status), str(c.count), c.detail)
        out.print(t)
        for c in checks:
            if c.status != "PASS" and c.examples and examples:
                out.print(f"[bold]{c.name}[/] — first {min(examples, len(c.examples))}:")
                for ex in c.examples[:examples]:
                    out.print("  " + ", ".join(f"{k}={fmt(v)}" for k, v in ex.items()))
        mt = table("Missing values by parameter (% of runs)", "parameter", ("missing %", {"justify": "right"}),
                   "parameter", ("missing %", {"justify": "right"}))
        items = list(extra["missing_pct"].items())
        half = (len(items) + 1) // 2
        for (p1, v1), right in zip(items[:half], items[half:] + [("", None)]):
            mt.add_row(p1, f"{v1:.1f}", right[0], "" if right[1] is None else f"{right[1]:.1f}")
        out.print(mt)
        out.print(f"overall: {styled(worst_status)}")
    raise typer.Exit({"PASS": 0, "WARN": 1, "FAIL": 2}[worst_status])


# --------------------------------------------------------------------------- fleet list


@fleet_app.command("list")
def fleet_list(
    ctx: typer.Context,
    station: Optional[str] = typer.Option(None, "--station", help="Filter by station (case-insensitive substring)."),
    capacity_class: Optional[str] = typer.Option(None, "--class", help="Filter by capacity class, e.g. C45."),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by condition: OK|WARN|ALARM|NA."),
    sort: str = typer.Option("id", "--sort", help="Sort by: id | status."),
    as_of: Optional[str] = AsOfOpt,
    as_json: bool = JsonOpt,
) -> None:
    """One row per fan: runs, run-hours, condition vs limits, OEM PM overdue, failures."""
    from .views import fleet_rows

    home = _home(ctx)
    conn = _connect(home, as_json)
    ref, ref_src = resolve_as_of(conn, as_of)
    rows = fleet_rows(conn, load_limits(home.limits_path), ref)
    if station:
        rows = [r for r in rows if station.lower() in r["station"].lower()]
    if capacity_class:
        rows = [r for r in rows if r["capacity_class"].lower() == capacity_class.lower()]
    if status:
        rows = [r for r in rows if r["condition"] == status.upper()]
    if sort == "status":
        order = {"ALARM": 0, "WARN": 1, "OK": 2, "NA": 3}
        rows.sort(key=lambda r: (order[r["condition"]], r["fan_id"]))
    if as_json:
        emit_json({"as_of": ref.isoformat(), "as_of_source": ref_src, "fans": rows})
        _exit(max((CONDITION_EXIT[r["condition"]] for r in rows), default=0))
        return
    clip = {"no_wrap": True, "overflow": "ellipsis"}

    def fixed(w: int, justify: str = "left") -> dict:
        return {"no_wrap": True, "min_width": w, "justify": justify}

    def pm_cell(r: dict) -> str:
        return f"{r['pm_overdue']}/{r['pm_tasks']}" + (f" {r['pm_overdue_at_height']}⇡" if r["pm_overdue_at_height"] else "")

    # (header, column options, cell, minimum terminal width to show the column) — narrow
    # terminals (e.g. 80-col SSH sessions) drop the least important columns instead of cropping.
    cols = [
        ("fan", fixed(7), lambda r: r["fan_id"], 0),
        ("station", {**clip, "max_width": 18}, lambda r: r["station"], 100),
        ("cls", fixed(3), lambda r: r["capacity_class"], 0),
        ("inst", fixed(4, "right"), lambda r: r["install_date"][:4], 110),
        ("runs", fixed(4, "right"), lambda r: str(r["runs"]), 0),
        ("run-h", fixed(5, "right"), lambda r: fmt(r["run_hours"], 1), 100),
        ("last run", fixed(10), lambda r: r["last_run"] or "—", 0),
        ("condition", fixed(9), lambda r: styled(r["condition"]), 0),
        ("flagged", {**clip, "max_width": 26}, lambda r: ", ".join(r["flagged_parameters"]), 0),
        ("susp", fixed(4, "right"), lambda r: str(len(r["suspect_parameters"]) or ""), 0),
        ("PM od", fixed(6, "right"), pm_cell, 0),
        ("fail", fixed(4, "right"), lambda r: str(r["failures"]), 0),
    ]
    cols = [c for c in cols if out.width >= c[3]]
    flexible = [c for c in cols if "max_width" in c[1]]
    used = sum(c[1]["min_width"] for c in cols if c not in flexible) + 3 * len(cols)
    spare = max(8 * len(flexible), out.width - used - 1)
    for c in flexible:  # share the leftover width; truncate with an ellipsis
        c[1]["max_width"] = min(c[1]["max_width"], spare // len(flexible))
    t = table(f"Fleet — {len(rows)} fans (as of {ref}, {ref_src})", *[(c[0], c[1]) for c in cols],
              box=box.SIMPLE_HEAD, pad_edge=False)
    for r in rows:
        t.add_row(*(c[2](r) for c in cols))
    out.print(t)
    counts = {s: sum(r["condition"] == s for r in rows) for s in ("ALARM", "WARN", "OK", "NA")}
    out.print("  ".join(f"{styled(k)} {v}" for k, v in counts.items())
              + f"   {styled('SUSPECT')} values {sum(len(r['suspect_parameters']) for r in rows)}")
    out.print("[dim]condition = latest trustworthy readings vs limits.yaml; susp. = latest values that jump "
              "sharply vs the fan's history (re-measure; excluded from condition); "
              "PM od = OEM PM tasks overdue/total, ⇡ = of which need work at height[/]")
    _placeholder_banner(home)
    _exit(max((CONDITION_EXIT[r["condition"]] for r in rows), default=0))


# --------------------------------------------------------------------------- fan show


@fan_app.command("show")
def fan_show(
    ctx: typer.Context,
    fan_id: str = typer.Argument(..., help="Fan ID, e.g. TBF-001."),
    runs: int = typer.Option(12, "--runs", min=2, help="Runs to include in trend sparklines."),
    as_of: Optional[str] = AsOfOpt,
    as_json: bool = JsonOpt,
) -> None:
    """Nameplate, latest readings vs limits with trends, OEM PM status and history."""
    from .views import fan_detail

    home = _home(ctx)
    conn = _connect(home, as_json)
    ref, _ = resolve_as_of(conn, as_of)
    d = fan_detail(conn, fan_id.upper(), load_limits(home.limits_path), ref, n_runs=runs)
    if d is None:
        _fail(f"unknown fan {fan_id!r} (see `tbfmon fleet list`)", as_json)
    if as_json:
        emit_json(d)
        _exit(CONDITION_EXIT[d["condition"]])
        return

    f = d["fan"]
    info = (
        f"[bold]{f['fan_id']}[/]  {f['station']} ({f['line']} line) · {f['tunnel_section']}\n"
        f"class {f['capacity_class']} · {fmt(f['rated_power_kw'])} kW · {fmt(f['rated_airflow_m3s'])} m³/s · "
        f"{fmt(f['rated_diff_pressure_pa'])} Pa · {fmt(f['rated_rpm'], 0)} rpm · {fmt(f['rated_voltage_v'], 0)} V · "
        f"{fmt(f['rated_current_a'])} A\n"
        f"OEM {f['oem_model']} · installed {f['install_date']} ({d['age_years']} y)\n"
        f"runs {d['runs']} ({', '.join(f'{k} {v}' for k, v in d['run_types'].items())}) · "
        f"run-hours {d['run_hours']} · last run {(d['last_run'] or '—')[:16]}\n"
        f"condition {styled(d['condition'])}"
        + (f" — {', '.join(d['flagged_parameters'])}" if d["flagged_parameters"] else "")
        + (f" · {styled('SUSPECT')} {', '.join(d['suspect_parameters'])}" if d["suspect_parameters"] else "")
        + f" · data-quality issues logged: {d['data_quality_issues']}"
    )
    out.print(Panel(info, title=f"Fan (as of {d['as_of']})", expand=False))

    dates = d["trend_run_dates"]
    rt = table(f"Latest readings vs limits · trend over last {len(dates)} runs "
               f"({dates[0] if dates else '—'} → {dates[-1] if dates else '—'})",
               "parameter", ("latest", {"justify": "right"}), "unit", ("ref*", {"justify": "right"}),
               "limit warn/alarm", "status", "trend")
    for r in d["readings"]:
        limit = (r["limit"] or "") + (" [dim]ph[/]" if r["limit_placeholder"] else "")
        latest, status = fmt(r["latest"]), styled(r["status"])
        if r["suspect"]:
            sp = r["suspect"]
            latest = f"[magenta]{fmt(sp['value'])}?[/]"
            prev = f"prev {fmt(r['latest'])} {r['status']}" if r["latest"] is not None else "no confirmed value"
            status = f"{styled('SUSPECT')} [dim]({prev})[/]"
        rt.add_row(r["label"], latest, r["unit"], fmt(r["reference_median_first5"]),
                   limit, status, sparkline(r["trend"]))
    out.print(rt)
    out.print("[dim]* ref = median of this fan's first 5 values held; ph = placeholder limit; · = not measured; "
              "SUSPECT = newest value jumps sharply vs history — re-measure before acting[/]")

    pt = table("OEM PM schedule (time-based)", "task", "interval", "height", "last done", "next due", "status")
    for t in d["pm"]:
        interval = " / ".join(x for x in (f"{t['interval_months']} mo" if t["interval_months"] else "",
                                          f"{fmt(t['interval_run_hours'], 0)} h" if t["interval_run_hours"] else "") if x)
        status = t["status"] + (f" {t['days_overdue']}d" if t["days_overdue"] else "")
        pt.add_row(t["task"], interval, "yes" if t["needs_work_at_height"] else "no",
                   t["last_done"] or "—", t["next_due"] or "—", styled(t["status"]).replace(t["status"], status))
    out.print(pt)

    mt = table("Maintenance history (latest 10)", "date", "type", "task", "parts", "findings")
    for m in d["maintenance"][-10:]:
        mt.add_row(m["date"], m["type"], m["task"], "; ".join(m["parts_replaced"]) or "—", m["findings"])
    out.print(mt)

    if d["failures"]:
        ft = table("Failures", "date", "mode", "symptoms", "root cause / fix")
        for x in d["failures"]:
            sym = "\n".join(f"{s['parameter']} {s['direction']}: {s['note']}" for s in x["symptoms"]) or "—"
            ft.add_row(x["date"], x["failure_mode"], sym, f"{x['root_cause']}\n[dim]fix:[/] {x['fix']}")
        out.print(ft)
    else:
        out.print("[dim]No failures recorded.[/]")
    _placeholder_banner(home)
    _exit(CONDITION_EXIT[d["condition"]])


if __name__ == "__main__":  # pragma: no cover
    app()
