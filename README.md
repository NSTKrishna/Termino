# tbfmon

Terminal-only condition-based monitoring for **Tunnel Booster Fans** (TBF) in
underground metro stations. It decides maintenance from each fan's actual
condition (test-run readings, permissible limits, maintenance history, past
failures) instead of fixed OEM intervals that need costly work at height.

Everything runs through the `tbfmon` command in a plain terminal, including over
SSH with no display. There is no web UI. Design, schema and decisions are in
[DESIGN.md](DESIGN.md), the source of truth.

> **Status:** foundation plus a layered, explainable analytics engine
> (limits → features → baselines/drift/peers → anomaly → fault modes → trend →
> Health Index → recommendations), evaluated on **simulated data only**.
> All shipped permissible limits are **placeholders** (REPLACE WITH OEM VALUES).

## Quick start

Requires Python 3.11+.

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'            # or: uv venv && uv pip install -e '.[dev]'

tbfmon init                                    # ./tbfmon_data: DB + editable limits.yaml
tbfmon simulate --fans 40 --years 3 --seed 42  # synthetic fleet → CSVs → DB
tbfmon ingest tbfmon_data/sim                  # re-ingest the CSVs: idempotent, 0 changes
tbfmon validate                                # data-quality report
tbfmon fleet list --sort status                # one row per fan
tbfmon fan show TBF-003                        # one fan in detail
tbfmon assess                                  # Health Index per fan, lowest first
tbfmon assess --fan TBF-040                    # full explanation for one fan
tbfmon recommend                               # maintenance plan vs OEM schedule, savings
tbfmon evaluate                                # SIMULATED DATA: vs ground truth and baselines
```

Every command accepts `--json` for machine-readable output, e.g.
`tbfmon fleet list --json | jq '.fans[] | select(.condition != "OK")'`.

## Commands

| command | what it does |
|---|---|
| `tbfmon init [--force]` | Create the workspace, SQLite schema and `limits.yaml`. |
| `tbfmon simulate --fans N --years Y --seed S` | Deterministic synthetic fleet: CSV bundle in `sim/`, ground truth in `ground_truth/` (never ingested), then ingest. |
| `tbfmon ingest PATH [--kind K]` | Ingest a CSV bundle directory or one CSV file. Re-running it is safe. |
| `tbfmon validate [--fail-on-warn]` | Limits, referential/temporal checks, glitches (spikes, ×10 slips, °F, stuck zeros), physics checks, missingness. |
| `tbfmon fleet list [--station S] [--class C] [--status S] [--sort status]` | Condition vs limits, suspect readings, OEM PM overdue (⇡ = at height), failures. |
| `tbfmon fan show ID [--runs N]` | Nameplate, latest readings vs limits with trend sparklines, PM due status, maintenance and failure history. |
| `tbfmon assess [--fan ID]` | Health Index 0–100 and status (Healthy/Watch/Degraded/Critical) with reasons; per fan: limit check with % deviation, baseline z / EWMA / CUSUM / peer z, anomaly contributors, fault modes (rules + model), time-to-ALARM with CI. |
| `tbfmon recommend [--all]` | Per fan: no action / defer OEM PM / targeted inspection / urgent corrective, by-date, tasks, work at height, reason codes; OEM vs CBM man-hours and work-at-height hours. |
| `tbfmon evaluate` | Only reader of `ground_truth.csv`. Recall, lead time, false alarms per fan-year, fault-mode precision/recall vs threshold-only and OEM baselines. Labelled SIMULATED DATA. |

Global options: `--home PATH` (or `TBFMON_HOME`) selects the workspace
(default `./tbfmon_data`). `--as-of YYYY-MM-DD|today` on `validate`, `fleet list`
and `fan show` sets the reference date; the default is the latest date in the data.

## Your own data

Put CSVs with the columns in DESIGN.md §3 into one directory (`fans.csv`,
`pm_schedule.csv`, `test_runs.csv`, `maintenance.csv`, `failures.csv`, any
subset) and run `tbfmon ingest DIR`. For a single file with another name, pass
`--kind test_run` (or `fan`, `pm_task`, `maintenance`, `failure`).
Impossible values (sentinels like -999, out-of-range) are nulled and logged, and
rows for unknown fans are rejected and logged. `tbfmon validate` shows both.
MQTT/Modbus sources are documented stubs in `src/tbfmon/sources/`.

Edit `tbfmon_data/limits.yaml` to put in OEM limits. Set `placeholder: false`
on each entry you replace. Engine thresholds, Health Index weights and the
man-hour assumptions behind the savings estimate are in `tbfmon_data/engine.yaml`
(see DESIGN.md §9; evaluation results and known weaknesses in §10).

## Tests

```bash
pytest
```

The tests cover schema validation, seed determinism (byte-identical files),
ingest idempotency (including simulator↔CSV equivalence), suspect-value rules,
PM due logic, every engine layer, fan-grouped folds, the evaluator's scoring,
the CLI, and ground-truth isolation (static checks plus a run of the whole
engine with ground-truth file access rigged to fail). The full suite takes a
few minutes because it trains the models.

## Troubleshooting

**`ModuleNotFoundError: No module named 'tbfmon'` after an editable install on
macOS.** Recent Python versions skip `.pth` files that have the macOS `hidden`
flag, and some setups (e.g. synced Desktop/Documents folders) set that flag on
everything inside `.venv`. Either run `chflags -R nohidden .venv`, or use a
regular install (`pip install .`) and reinstall after code changes. `pytest`
is not affected (it adds `src/` to the path itself).
