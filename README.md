# tbfmon (Termino Lite)

Terminal-only data and limit monitoring for **Tunnel Booster Fans (TBF)** in
underground metro stations (DMRC use case).

TBFs sit idle most of the time and only run during mock drills and tests. Each
run produces readings (vibration, temperatures, phase currents, voltage, rpm,
airflow, pressure, noise, insulation resistance), often hand-typed and messy.
`tbfmon` brings that data into one local database safely, checks its quality,
compares every reading with permissible limits and shows the condition of every
fan in the terminal.

Everything runs through the `tbfmon` command in a plain terminal, including
over SSH with no display. There is no web server or GUI. Design, schema and
decisions: [DESIGN.md](DESIGN.md).

> **Status:** Termino Lite: data model, storage, simulator, ingestion,
> validation and limit views. Tested on **simulated data only** (no real DMRC
> data exists yet). All permissible limits are **placeholders** until replaced
> with OEM values. The condition-based analytics engine is the next phase
> (DESIGN.md §8).

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
tbfmon init                                        # ./tbfmon_data: DB + limits.yaml
tbfmon simulate --fans 40 --years 3 --seed 42      # synthetic fleet history (no real data yet)
tbfmon ingest tbfmon_data/sim                      # re-ingest: nothing changes (idempotent)
tbfmon validate                                    # data-quality report
tbfmon fleet list                                  # every fan: condition vs limits, PM overdue
tbfmon fan show TBF-030                            # one fan: readings vs limits, sparklines, PM, history
```

Every command takes `--json`, and `--home PATH` (or `TBFMON_HOME`) selects the
workspace.

## Architecture

```
  CSV exports · simulator · (MQTT/Modbus stubs)
                    │   sources/ → ingest (sanitise → validate → idempotent upsert)
                    ▼
     ┌──────────────── SQLite  tbfmon.db ────────────────┐
     │ fans · PM tasks · runs · maintenance · failures · │
     │ data-quality issues · ingest batches              │
     └─────────────────────────┬─────────────────────────┘
                               ▼
  validate.py · limits.py · pm.py · views.py   (no UI code)
                               ▼
  CLI        Typer + Rich rendering  →  terminal tables · sparklines · --json

  simulate/ → CSV bundle + ground_truth.csv (kept separate, never read)
```

Only `cli.py` and `render.py` import Typer or Rich (enforced by a test), so the
data layer could be reused by another front end without changes.

## Commands

| command | what it does | exit code |
|---|---|---|
| `init [--force]` | create workspace, database and `limits.yaml` | 0 |
| `simulate --fans N --years Y --seed S` | synthetic fleet history (+ separate ground truth), then ingest | 0 |
| `ingest PATH [--kind K]` | load CSV bundle or file; re-running changes nothing | 0 |
| `validate` | data-quality report: glitches, physics checks, missing values | 0/1/2 = PASS/WARN/FAIL |
| `fleet list [--station S] [--class C] [--status S] [--sort id\|status]` | one row per fan: runs, run-hours, condition vs limits, OEM PM overdue, failures | worst condition |
| `fan show ID [--runs N]` | nameplate, readings vs limits with sparklines, PM due, maintenance and failure history | condition |

**Exit codes** for cron and SCADA scripts: `0` ok · `1` warn · `2` alarm ·
`3` error (bad input, unknown fan, missing file, not initialised).

```bash
# nightly: check the data, page someone on alarm
tbfmon fleet list --json >> /var/log/tbfmon.jsonl
[ $? -eq 2 ] && echo "TBF alarm" | mail -s "tbfmon ALARM" maint@example.invalid
```

## Configuration

| file / variable | what |
|---|---|
| `limits.yaml` | permissible warn/alarm limits per capacity class. **Placeholders:** replace with OEM values and set `placeholder: false` |
| `TBFMON_HOME` | workspace directory (default `./tbfmon_data`) |

## Your own data

CSV columns are listed in DESIGN.md §3. Put `fans.csv`, `pm_schedule.csv`,
`test_runs.csv`, `maintenance.csv` and `failures.csv` (any subset) in a folder
and run `tbfmon ingest DIR`. For a single file with another name, pass
`--kind test_run` (or another kind). Fan IDs like `12` or `tbf-12` are
accepted, and rows for unknown fans are rejected and reported.

## Limitations

- **Tested on simulated data only**, from a simulator whose physics the same
  project wrote. Real signatures, noise and instrument practice will differ.
- **All permissible limits are placeholders** (starting points from ISO 10816-3,
  IEEE 43, NEMA MG-1), not OEM or DMRC values.
- **Limit checks are not prediction.** A WARN or ALARM appears only once a
  reading is already over a limit. Early warning (drift, anomaly detection,
  fault diagnosis) is the next phase.
- **A single sharp jump is shown as "suspect" (re-measure)** unless a related
  parameter corroborates it, so a real fault can wait one run before it counts
  toward the condition.
- **Single user, single machine:** SQLite, no authentication.

## Tests

```bash
pytest
```

The tests cover schemas, the database, simulator determinism, ingest
idempotency, limits, data-quality checks, ground-truth isolation, UI-free
layering, and a CliRunner test for every command.

## Troubleshooting

**`ModuleNotFoundError: No module named 'tbfmon'` after an editable install on
macOS.** Recent Python versions skip `.pth` files that carry the macOS `hidden`
flag, and some setups (e.g. synced Desktop/Documents folders) set that flag on
everything in `.venv`. Run `chflags -R nohidden .venv`, or use `pipx install .`
(or `pip install .`) and reinstall after code changes. `pytest` is unaffected
because it adds `src/` to the path itself.

**Wide tables wrap.** Output adapts to the terminal width; at 80 columns some
low-priority columns are dropped. `--json` always has everything.
