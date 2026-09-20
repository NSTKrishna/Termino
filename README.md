# tbfmon

Terminal-only condition-based monitoring and maintenance for **Tunnel Booster
Fans (TBF)** in underground metro stations (DMRC use case).

TBFs sit idle and only run during mock drills and tests, while the OEM's
time-based preventive maintenance costs a lot of manpower and work at height.
`tbfmon` decides maintenance from each fan's actual condition instead: test-run
readings vs permissible limits, drift from the fan's own history and from its
peers, fault signatures, trends, and past maintenance and failures. Every
verdict comes with its reasons.

Everything runs through the `tbfmon` command in a plain terminal, including
over SSH with no display. There is no web server or GUI; HTML exists only as an
exported report file. Design, schema and decisions: [DESIGN.md](DESIGN.md).

> **Status:** working foundation, analytics engine, alerting and terminal UX,
> evaluated on **simulated data only** (no real DMRC data exists yet). All
> permissible limits are **placeholders** until replaced with OEM values.

## Install

Requires Python 3.11+.

```bash
pipx install .                 # isolated install of the `tbfmon` command
# or, for development:
python3.11 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
```

`python -m tbfmon …` works too.

## Quick start

```bash
tbfmon demo                    # 2–3 minute guided story (add --fast to skip pauses)
```

Or step by step:

```bash
tbfmon init                                        # ./tbfmon_data: DB + limits/engine/config YAML
tbfmon simulate --fans 40 --years 3 --seed 42      # synthetic fleet history (no real data yet)
tbfmon fleet                                       # health of every fan, worst first, with trends
tbfmon drill --station "Rajiv Chowk" --simulate --inject TBF-030:bearing_wear
tbfmon alerts list                                 # what the drill raised
tbfmon explain --fan TBF-030                       # why: features, rules, similar past failures
tbfmon analyze --fan TBF-030                       # every parameter vs its limit, deviation %
tbfmon plan --horizon 90d                          # CBM plan by station vs the OEM schedule
tbfmon report --fleet --format html                # static HTML file in tbfmon_data/reports/
tbfmon alerts ack 3 --note "bearing ordered"       # acknowledge (user + IST timestamp)
```

Every command takes `--json`, and `--home PATH` (or `TBFMON_HOME`) selects the
workspace.

## Architecture

```
  CSV exports · simulated drills · watched folder · (MQTT/Modbus stubs)
                    │   sources/ → ingest (sanitise → validate → idempotent upsert)
                    ▼
     ┌──────────────── SQLite  tbfmon.db ────────────────┐
     │ fans · runs · maintenance · failures · alerts ·   │
     │ watched files · data-quality issues               │
     └─────────────────────────┬─────────────────────────┘
                               ▼
  engine/    clean → limits → features → baseline/drift/peers → anomaly (IsolationForest)
             → fault modes (signature rules + RandomForest) → trend → health index → recommend
                               ▼
  services/  analyze · fleet · explain · drill · watch · plan · alerts · reports   (no UI code)
                               ▼
  CLI        Typer + Rich rendering  →  terminal · --json · report files (md/html/csv/json)

  simulate/ → CSV bundle + ground_truth.csv → evaluation/ (the only reader of ground truth)
```

The engine and services never import the CLI, Typer or Rich (enforced by a
test), so another front end could reuse them without touching the engine.

## Commands

| command | what it does | exit code |
|---|---|---|
| `init [--force]` | create workspace, database, `limits.yaml`, `engine.yaml`, `config.yaml` | 0 |
| `simulate --fans N --years Y --seed S` | synthetic fleet history (+ separate ground truth) | 0 |
| `ingest PATH [--kind K]` | load CSV bundle or file; re-running changes nothing | 0 |
| `validate` | data-quality report: glitches, physics checks, missing values | 0/1/2 = PASS/WARN/FAIL |
| `fleet [--station S] [--capacity C] [--status S]` | health table with sparklines, worst first | worst status |
| `fleet list` · `fan show ID` | limit-condition views, OEM PM due status | worst condition |
| `analyze --fan ID [--run N \| --latest]` | parameter · measured · limit · deviation % · status, health bar, fault modes, action | status |
| `explain --fan ID` | contributing features, rule reasons, similar past failures | status |
| `drill --station S (--file CSV \| --simulate)` | mock drill: ingest, analyse each fan, risk-sorted summary, alerts | worst |
| `watch DIR [--once] [--simulate-feed]` | live view; picks up reading files as they land; alerts in real time | worst seen |
| `alerts list` · `alerts ack ID… [--all]` | persisted alerts (INFO/WARN/ALARM/CRITICAL), dedupe, cooldown | worst listed |
| `plan --horizon 90d [--station S]` | CBM plan by station, work at height flagged, deferred OEM PMs, man-hours saved | 2 urgent · 1 inspect · 0 |
| `report (--fan ID \| --fleet) --format md\|html\|csv\|json` | report file (`--out -` for stdout) | 0 |
| `assess` · `recommend` | engine-level health and recommendation tables | status / action |
| `evaluate` | score against simulator ground truth — SIMULATED DATA | 0 |
| `demo [--fast]` | scripted 2–3 minute story in `./tbfmon_demo` | 0 |

**Exit codes** for cron and SCADA scripts: `0` ok · `1` warn · `2` alarm ·
`3` error (bad input, unknown fan or station, missing file, not initialised).

```bash
# nightly: process whatever landed, page someone on alarm
tbfmon watch /data/tbf/incoming --once --json >> /var/log/tbfmon.jsonl
[ $? -eq 2 ] && echo "TBF alarm" | mail -s "tbfmon ALARM" maint@example.invalid
```

## Configuration

| file / variable | what |
|---|---|
| `limits.yaml` | permissible warn/alarm limits per capacity class. **Placeholders:** replace with OEM values and set `placeholder: false` |
| `engine.yaml` | engine thresholds, Health Index weights, deferral rules, man-hour assumptions behind savings |
| `config.yaml` | timezone (default IST), alert cooldown, JSONL log, notifier stubs, watch interval |
| `TBFMON_HOME` | workspace directory (default `./tbfmon_data`) |
| `TBFMON_TZ`, `TBFMON_USER` | display timezone; who acknowledges alerts |
| `TBFMON_ALERT_COOLDOWN_HOURS`, `TBFMON_NOTIFY_DRY_RUN` | alert cooldown; notifier dry-run on/off |
| `TBFMON_WATCH_INTERVAL` | `watch` polling interval in seconds |

**Alerts** are stored in SQLite and deduplicated while open. A higher severity
escalates an alert and notifies again, and acknowledged alerts stay quiet for
the cooldown unless they get worse. Each alert goes to the terminal and
`alerts.jsonl`. Email, SMS and webhook are **stubs** that write to
`outbox.jsonl` in dry-run and never send anything over the network.

## Your own data

CSV columns are listed in DESIGN.md §3. Put `fans.csv`, `pm_schedule.csv`,
`test_runs.csv`, `maintenance.csv` and `failures.csv` (any subset) in a folder
and run `tbfmon ingest DIR`. For a single file with another name, pass
`--kind test_run` (or another kind). Drill files for `drill --file` and
`watch` need `fan_id,timestamp,run_type,duration_min` plus any measurement
columns. Fan IDs like `12` or `tbf-12` are accepted, and rows for unknown fans
or other stations are skipped and reported.

## Limitations — read before trusting a number

- **Everything is validated on simulated data only**, from a simulator whose
  physics the same project wrote. Real signatures, noise, failure rates and
  instrument practice will differ.
- **All permissible limits are placeholders** (starting points from ISO 10816-3,
  IEEE 43, NEMA MG-1), not OEM or DMRC values.
- **Sparse testing is the hard limit.** Faults that develop between runs, or
  with the key parameter unmeasured (IR on ~35 % of runs, airflow on <50 %),
  are missed. Sudden control-circuit failures have no precursor and are never
  predicted.
- **Detection versus plain limit alarms (simulated):** clearly fewer false
  alarms (~0.1 vs ~0.6 per fan-year) and usually earlier warning, but only
  modestly higher recall. Lubrication loss and airflow restriction are the
  weakest modes; see DESIGN.md §10.
- **The supervised model is weak with few failures.** It is shown and blended,
  but the transparent rules carry the decisions until many failures are recorded.
- **A single sharp jump is held as "suspect" (re-measure)** unless a related
  parameter corroborates it. A real fault can therefore wait one run, which
  trades a little delay for far fewer false alarms from glitches.
- **Savings are estimates.** They come from the man-hour assumptions in
  `engine.yaml`, count avoided PM visits only (not prevented failures), and
  assume fans stay healthy over the horizon.
- **Simulated time:** simulated drills and feeds are dated the night after the
  latest data, not today, so PM schedules stay meaningful for historical data.
- **Notifiers are stubs.** Nothing is emailed, texted or posted until a
  transport is deliberately implemented.
- **Single user, single machine:** SQLite, no authentication, and the ack
  user is taken from config, env or the OS login.

## Tests

```bash
pytest
```

The tests cover schemas, simulator determinism, ingest idempotency, every
engine layer, fan-grouped folds, the evaluator, ground-truth isolation, the
engine/services/CLI layering, a CliRunner test for every command, and an
end-to-end run of the demo. The full suite takes about 2 minutes because it
trains the models.

## Troubleshooting

**`ModuleNotFoundError: No module named 'tbfmon'` after an editable install on
macOS.** Recent Python versions skip `.pth` files that carry the macOS `hidden`
flag, and some setups (e.g. synced Desktop/Documents folders) set that flag on
everything in `.venv`. Run `chflags -R nohidden .venv`, or use `pipx install .`
(or `pip install .`) and reinstall after code changes. `pytest` is unaffected
because it adds `src/` to the path itself.

**Wide tables wrap.** Output adapts to the terminal width; at 80 columns some
low-priority columns are dropped. `--json` always has everything.
