"""Engine commands: `assess`, `recommend`, `evaluate`. Terminal only (DESIGN.md §0)."""

from __future__ import annotations

from typing import Optional

import typer
from rich import box
from rich.panel import Panel

from .render import emit_json, err, fmt, out, styled, table

HEALTH_STYLE = {"Healthy": "green", "Watch": "yellow", "Degraded": "dark_orange", "Critical": "bold red"}
ACTION_STYLE = {"no action": "dim", "defer OEM PM": "green", "targeted inspection": "yellow",
                "urgent corrective": "bold red"}


def _h(status: str) -> str:
    return f"[{HEALTH_STYLE.get(status, '')}]{status}[/]"


def _a(action: str) -> str:
    return f"[{ACTION_STYLE.get(action, '')}]{action}[/]"


def register(app: typer.Typer) -> None:
    from .cli import AsOfOpt, JsonOpt, _connect, _exit, _fail, _home, _placeholder_banner
    from .services.errors import STATUS_EXIT

    def _load(ctx: typer.Context, as_of: str | None, as_json: bool, mode: str = "full"):
        from .engine import assess, load_engine_config, load_fleet
        from .limits import load_limits
        from .pm import resolve_as_of

        home = _home(ctx)
        conn = _connect(home, as_json)
        ref, _ = resolve_as_of(conn, as_of)
        cfg = load_engine_config(home.engine_config_path)
        data = load_fleet(conn, load_limits(home.limits_path), ref)
        if data.runs.empty:
            _fail("no test runs in the database — ingest data first", as_json)
        if not as_json:
            err.print(f"[dim]assessing {len(data.fans)} fans, {len(data.runs)} runs as of {ref}…[/]")
        return home, conn, cfg, assess(data, cfg, mode)

    # ------------------------------------------------------------------ assess
    @app.command()
    def assess(
        ctx: typer.Context,
        fan: Optional[str] = typer.Option(None, "--fan", help="Explain one fan in full."),
        as_of: Optional[str] = AsOfOpt,
        as_json: bool = JsonOpt,
    ) -> None:
        """Health index, status, fault modes, drift, anomaly and trends — with the reasons."""
        from .engine.pipeline import explain_run, fan_trends_all_axes, recommend_all

        home, _, cfg, a = _load(ctx, as_of, as_json)
        recs = {r.fan_id: r for r in recommend_all(a)}
        if fan:
            fid = fan.upper()
            i = a.latest_index(fid)
            if i is None:
                _fail(f"unknown fan or no runs: {fan!r}", as_json)
            detail = explain_run(a, i)
            detail["trends_all_axes"] = [t.to_dict() for t in fan_trends_all_axes(a, fid)]
            detail["recommendation"] = recs[fid].to_dict()
            detail["model_info"] = a.info
            if as_json:
                emit_json(detail)
            else:
                _print_fan(detail, cfg)
                _placeholder_banner(home)
            _exit(STATUS_EXIT[detail["health"]["status"]])
            return

        rows = []
        for fid in a.data.fans.index:
            i = a.latest_index(fid)
            if i is None:
                continue
            r = a.table.loc[i]
            h = a.health[i]
            rows.append({
                "fan_id": fid, "capacity_class": a.data.fans.loc[fid, "capacity_class"],
                "last_run": r["ts"].date().isoformat(), "health_index": h.index, "status": h.status,
                "components": h.components, "reasons": h.reasons,
                "top_mode": a.combined[i][0][0] if a.combined[i] else None,
                "top_mode_confidence": a.combined[i][0][1] if a.combined[i] else 0.0,
                "anomaly_percentile": None if r["anomaly_pct"] != r["anomaly_pct"] else round(float(r["anomaly_pct"]), 3),
                "trend": ({"indicator": r["trend_indicator"], "eta_days": round(float(r["trend_eta_days"]), 0)}
                          if isinstance(r["trend_indicator"], str) else None),
                "baseline_established": bool(r["baseline_established"]),
                "action": recs[fid].action,
            })
        rows.sort(key=lambda x: x["health_index"])
        code = max((STATUS_EXIT[x["status"]] for x in rows), default=0)
        if as_json:
            emit_json({"as_of": a.data.as_of.isoformat(), "fans": rows, "model_info": a.info})
            _exit(code)
            return
        t = table(f"Fleet health (as of {a.data.as_of}) — lowest first", "fan", "cls",
                  ("HI", {"justify": "right"}), "status", "likely fault (conf)", ("anom", {"justify": "right"}),
                  "trend → ALARM", "action", box=box.SIMPLE_HEAD, pad_edge=False)
        for x in rows:
            mode = f"{x['top_mode']} ({x['top_mode_confidence']:.2f})" if x["top_mode_confidence"] >= 0.3 else ""
            tr = (f"{x['trend']['indicator']} ~{x['trend']['eta_days']:.0f}d"
                  if x["trend"] and x["trend"]["eta_days"] <= 3 * 365 else "")
            anom = "" if x["anomaly_percentile"] is None else f"{x['anomaly_percentile']:.2f}"
            t.add_row(x["fan_id"], x["capacity_class"], f"{x['health_index']:.0f}", _h(x["status"]), mode,
                      anom, tr, _a(x["action"]))
        out.print(t)
        counts = {s: sum(x["status"] == s for x in rows) for s in HEALTH_STYLE}
        out.print("  ".join(f"{_h(k)} {v}" for k, v in counts.items()))
        for w in a.info["supervised"].get("warnings", []):
            err.print(f"[dim]model: {w}[/]")
        out.print("[dim]HI = 100 − weighted penalties (limits 30, drift 20, anomaly 15, fault mode 15, trend 15, "
                  "peers 5; engine.yaml). `tbfmon assess --fan ID` explains one fan.[/]")
        _placeholder_banner(home)
        _exit(code)

    # ------------------------------------------------------------------ recommend
    @app.command()
    def recommend(
        ctx: typer.Context,
        all_fans: bool = typer.Option(False, "--all", help="Include fans with no action."),
        as_of: Optional[str] = AsOfOpt,
        as_json: bool = JsonOpt,
    ) -> None:
        """Maintenance plan per fan vs the OEM schedule, with estimated savings."""
        from .engine.pipeline import recommend_all
        from .engine.recommend import totals

        home, _, cfg, a = _load(ctx, as_of, as_json)
        recs = recommend_all(a)
        tot = totals(recs)
        assumptions = {"horizon_days": cfg.recommender.horizon_days, "man_hours": cfg.recommender.man_hours,
                       "defer_factor": cfg.recommender.defer_factor,
                       "max_interval_factor": cfg.recommender.max_interval_factor,
                       "note": "prevented-failure benefits not counted"}
        code = 2 if tot["actions"]["urgent corrective"] else 1 if tot["actions"]["targeted inspection"] else 0
        if as_json:
            emit_json({"as_of": a.data.as_of.isoformat(), "recommendations": [r.to_dict() for r in recs],
                       "totals": tot, "assumptions": assumptions})
            _exit(code)
            return
        order = {"urgent corrective": 0, "targeted inspection": 1, "defer OEM PM": 2, "no action": 3}
        shown = [r for r in sorted(recs, key=lambda r: (order[r.action], r.health_index or 0))
                 if all_fans or r.action != "no action"]
        t = table(f"Maintenance recommendations (as of {a.data.as_of})", "fan", "status", ("HI", {"justify": "right"}),
                  "action", "by", "height", "tasks", "reasons", box=box.SIMPLE_HEAD, pad_edge=False)
        for r in shown:
            tasks = "\n".join(r.tasks[:3]) + (f"\n… +{len(r.tasks) - 3} more" if len(r.tasks) > 3 else "")
            t.add_row(r.fan_id, _h(r.status), fmt(r.health_index, 0), _a(r.action), r.by_date or "—",
                      "yes" if r.needs_work_at_height else "no", tasks,
                      "\n".join([c for c in r.reason_codes if not c.startswith("LIMITED")][:2]))
        out.print(t)
        s = table(f"Next {cfg.recommender.horizon_days} days: OEM time-based vs condition-based (estimate)",
                  "", ("OEM", {"justify": "right"}), ("CBM", {"justify": "right"}), ("saved", {"justify": "right"}))
        s.add_row("PM task occurrences", str(tot["oem_tasks_in_horizon"]), str(tot["cbm_tasks_in_horizon"]),
                  str(tot["oem_tasks_in_horizon"] - tot["cbm_tasks_in_horizon"]))
        s.add_row("man-hours", fmt(tot["man_hours_oem"], 0), fmt(tot["man_hours_cbm"], 0), fmt(tot["man_hours_saved"], 0))
        s.add_row("work-at-height hours", fmt(tot["height_hours_oem"], 0), fmt(tot["height_hours_cbm"], 0),
                  fmt(tot["height_hours_saved"], 0))
        out.print(s)
        out.print("  ".join(f"{_a(k)} {v}" for k, v in tot["actions"].items()))
        mh = cfg.recommender.man_hours
        out.print(f"[dim]Assumptions (engine.yaml): {mh['at_height']:g} h per at-height task, {mh['ground']:g} h ground, "
                  f"{mh['targeted_inspection']:g} h per inspection, {mh['urgent_corrective']:g} h per corrective; "
                  f"deferral ≤ {cfg.recommender.max_interval_factor:g}× interval; nothing deferred without a "
                  f"baseline or with data older than {cfg.recommender.stale_after_days} d; IR tests and control "
                  f"checks never deferred; prevented-failure benefit not counted.[/]")
        _placeholder_banner(home)
        _exit(code)

    # ------------------------------------------------------------------ evaluate
    @app.command()
    def evaluate(
        ctx: typer.Context,
        as_of: Optional[str] = AsOfOpt,
        as_json: bool = JsonOpt,
    ) -> None:
        """Score the engine against simulator ground truth (SIMULATED DATA only)."""
        from .engine import load_engine_config
        from .evaluation import evaluate as run_evaluation
        from .limits import load_limits
        from .pm import resolve_as_of

        home = _home(ctx)
        conn = _connect(home, as_json)
        ref, _ = resolve_as_of(conn, as_of)
        if not as_json:
            err.print("[dim]evaluating point-in-time (fan-held-out models)…[/]")
        try:
            res = run_evaluation(conn, home.ground_truth_dir, load_limits(home.limits_path),
                                 load_engine_config(home.engine_config_path), ref)
        except FileNotFoundError as e:
            _fail(str(e), as_json)
        if as_json:
            emit_json(res)
            return
        _print_evaluation(res)


# ---------------------------------------------------------------------- rendering


def _print_fan(d: dict, cfg) -> None:
    h = d["health"]
    rec = d["recommendation"]
    comp = "  ".join(f"{k} −{cfg.health_index.weights[k] * v:.0f}" for k, v in h["components"].items() if v)
    body = (f"[bold]{d['fan_id']}[/]  last run {d['ts'][:16]} ({d['run_type']})\n"
            f"Health Index [bold]{h['index']:.0f}[/]/100 · {_h(h['status'])}"
            + (f"   penalties: {comp}" if comp else "") + "\n"
            f"reasons: {', '.join(h['reasons']) or '—'}\n"
            f"→ {_a(rec['action'])}" + (f" by {rec['by_date']}" if rec["by_date"] else "")
            + (" · work at height" if rec["needs_work_at_height"] else ""))
    for task in rec["tasks"]:
        body += f"\n   • {task}"
    out.print(Panel(body, title="Condition assessment", expand=False))

    fm = d["fault_modes"]
    if fm["rules"] or fm["model"]:
        t = table("Fault modes — transparent rules (primary) + model (secondary)", "mode",
                  ("rules", {"justify": "right"}), ("model", {"justify": "right"}), ("combined", {"justify": "right"}), "why")
        comb = {c["mode"]: c["confidence"] for c in fm["combined"]}
        rules = {r["mode"]: r for r in fm["rules"]}
        for m in list(dict.fromkeys([c["mode"] for c in fm["combined"]] + list(rules)))[:4]:
            r = rules.get(m, {})
            t.add_row(m + (f" ({r['location']})" if r.get("location") else ""), fmt(r.get("confidence")),
                      fmt(fm["model"].get(m)), fmt(comb.get(m)), "\n".join(r.get("reasons", [])))
        out.print(t)

    lim = [x for x in d["limits"] if x["status"] in ("WARN", "ALARM", "SUSPECT")]
    lim += sorted([x for x in d["limits"] if x["status"] == "OK" and x["pct_deviation"] is not None],
                  key=lambda x: -x["pct_deviation"])[: max(0, 4 - len(lim))]
    if lim:
        t = table("Limit check (worst first)", "parameter", ("value", {"justify": "right"}), "basis",
                  ("warn / alarm", {"justify": "right"}), ("% vs WARN", {"justify": "right"}), "status")
        for x in lim:
            t.add_row(x["parameter"], fmt(x["value"]), x["basis"], f"{fmt(x['warn'])} / {fmt(x['alarm'])} ({x['side']})",
                      "" if x["pct_deviation"] is None else f"{x['pct_deviation']:+.0f}%", styled(x["status"]))
        out.print(t)

    dr = sorted([x for x in d["drift"] if x["z"] is not None or x["peer_z"] is not None],
                key=lambda x: -max(abs(x["z"] or 0), abs(x["peer_z"] or 0) * 0.7))[:8]
    if dr:
        t = table("Baseline, drift and peers (most deviating features)", "feature", ("value", {"justify": "right"}),
                  ("baseline", {"justify": "right"}), ("z", {"justify": "right"}), ("EWMA", {"justify": "right"}),
                  ("CUSUM", {"justify": "right"}), "drift", ("peer z", {"justify": "right"}), ("Δ prev", {"justify": "right"}))
        for x in dr:
            t.add_row(x["feature"], fmt(x["value"]), fmt(x["baseline"]), _z(x["z"]), fmt(x["ewma"]), fmt(x["cusum"]),
                      "[red]yes[/]" if x["flag"] else "", _z(x["peer_z"]), fmt(x["delta_prev"]))
        out.print(t)

    an = d["anomaly"]
    if an["percentile"] is not None:
        contrib = ", ".join(f"{c['feature']} (z={c['z']:+.1f})" for c in an["top_contributors"]) or "—"
        flag = "[red]anomalous[/]" if an["flagged"] else "typical"
        out.print(f"Anomaly (Isolation Forest per capacity class): "
                  f"percentile {an['percentile']:.2f} vs healthy runs → {flag}; top contributors: {contrib}")
    else:
        out.print("[dim]Anomaly: not scored (baseline not established or too few healthy training runs)[/]")

    t = table("Degradation trends (Theil–Sen on the current segment; time to ALARM with CI)", "indicator", "status",
              ("n", {"justify": "right"}), ("slope / day", {"justify": "right"}), ("calendar", {"justify": "right"}),
              ("run-hours", {"justify": "right"}), ("starts", {"justify": "right"}))
    by: dict[str, dict] = {}
    for x in d["trends_all_axes"]:
        by.setdefault(x["indicator"], {})[x["axis"]] = x
    for ind, ax in by.items():
        day = ax["days"]
        t.add_row(ind, day["status"], str(day["n"]), "" if day["slope"] is None else f"{day['slope']:+.3g}",
                  _eta(day, "d"), _eta(ax.get("run_hours_cum"), "h"), _eta(ax.get("starts_cum"), ""))
    out.print(t)
    for w in d["model_info"]["supervised"].get("warnings", []):
        err.print(f"[dim]model: {w}[/]")


def _eta(x: dict | None, unit: str) -> str:
    if not x or x["eta"] is None:
        return ""
    lo, hi = x["eta_ci"]
    hi_s = "∞" if hi is None or hi > 1e6 else f"{hi:.0f}"
    return f"{x['eta']:.0f}{unit} ({lo:.0f}–{hi_s})"


def _z(v) -> str:
    if v is None:
        return ""
    s = f"{v:+.1f}"
    return f"[red]{s}[/]" if abs(v) >= 3 else s


def _print_evaluation(r: dict) -> None:
    out.print(Panel(f"[bold yellow]{r['label']}[/]\nas of {r['as_of']} · {r['dataset']['fans']} fans · "
                    f"{r['dataset']['runs']} runs · {r['dataset']['fan_years']} fan-years · episodes "
                    f"{r['dataset']['episodes_by_outcome']} · sudden failures without precursor: "
                    f"{r['dataset']['sudden_failures_no_precursor']}", title="tbfmon evaluate", expand=False))
    t = table("Detection (SIMULATED DATA) — failures with precursors", "system", ("caught", {"justify": "right"}),
              ("recall", {"justify": "right"}), ("lead median (min–max) d", {"justify": "right"}),
              ("all episodes", {"justify": "right"}), ("false alarms / fan-yr", {"justify": "right"}),
              ("alert precision", {"justify": "right"}))
    for name, s in r["systems"].items():
        ld = s["lead_days"]
        lead = "—" if ld["median"] is None else f"{ld['median']:.0f} ({ld['min']}–{ld['max']})"
        t.add_row(name, f"{s['failures_detected']}/{s['failures']}", fmt(s["failure_recall"]), lead,
                  f"{s['episodes_detected']}/{s['episodes']}", fmt(s["false_alarms_per_fan_year"]),
                  fmt(s["alert_event_precision"]))
    oem = r["oem_baseline"]
    t.add_row("OEM time-based PM (as simulated)", f"{oem['caught_by_pm']}/{oem['episodes_ended']} ep.",
              fmt(oem["recall"]), "—", "—", f"{oem['pm_tasks_per_fan_year']:g} visits", "—")
    out.print(t)
    cbm = r["systems"]["CBM engine (inspect/urgent)"]
    thr = r["systems"]["Threshold-only (WARN or ALARM)"]
    m = table("Failures caught by mode (SIMULATED DATA)", "mode", ("CBM", {"justify": "right"}),
              ("threshold", {"justify": "right"}))
    for mode, v in cbm["failure_recall_by_mode"].items():
        m.add_row(mode, v, thr["failure_recall_by_mode"].get(mode, "—"))
    out.print(m)

    fm = r["fault_modes"]
    t = table("Fault-mode identification at the last alert of each detected episode (SIMULATED DATA)", "mode",
              ("n", {"justify": "right"}), "combined P / R", "rules P / R", "model P / R")
    pr = lambda v: f"{fmt(v['precision'])} / {fmt(v['recall'])}"  # noqa: E731
    for mode in [k for k in fm["combined_last"] if not k.startswith("_")]:
        t.add_row(mode, str(fm["combined_last"][mode]["support"]), pr(fm["combined_last"][mode]),
                  pr(fm["rules_last"][mode]), pr(fm["model_last"][mode]))
    out.print(t)
    o = {k: fm[k]["_overall"] for k in ("combined_first", "combined_last", "rules_last", "model_last")}
    out.print("mode named & correct: " + "  ".join(
        f"{k}: {v['mode_named']}/{v['detected_episodes']} named, {fmt(v['accuracy_when_named'])} correct" for k, v in o.items()))
    for w in r["supervised_warnings"]:
        out.print(f"[dim]model: {w}[/]")

    s = r["recommender_savings_next_year"]
    out.print(f"Cost: OEM PM as simulated {oem['pm_man_hours_per_fan_year']:g} man-h/fan-yr "
              f"({oem['pm_height_hours_per_fan_year']:g} at height), {oem['pm_tasks_with_normal_findings_pct']:g}% of PM "
              f"visits found nothing. CBM inspections triggered: {r['cbm_inspection_hours_per_fan_year']:g} man-h/fan-yr. "
              f"Recommender, next year: {s['man_hours_saved']:g} man-h saved "
              f"({s['per_fan_year']['man_hours_saved']:g}/fan), {s['height_hours_saved']:g} h at height saved.")

    t = table("Per failure (SIMULATED DATA)", "fan", "mode", "failure", ("runs", {"justify": "right"}),
              ("CBM lead d", {"justify": "right"}), "CBM mode (first → last)", ("threshold lead d", {"justify": "right"}))
    for p in r["per_failure"]:
        modes = f"{p['cbm_mode_at_first_alert'] or '—'} → {p['cbm_mode_at_last_alert'] or '—'}" if p["cbm_first_alert"] else ""
        t.add_row(p["fan_id"], p["mode"], p["failure"], str(p["runs_before_failure"]),
                  "missed" if p["cbm_lead_days"] is None else str(p["cbm_lead_days"]), modes,
                  "missed" if p["threshold_lead_days"] is None else str(p["threshold_lead_days"]))
    out.print(t)
    out.print(f"[bold yellow]{r['label']}[/]")
