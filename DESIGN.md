# tbfmon (Termino Lite) — Design

Data and limit monitoring for Tunnel Booster Fans (TBF) in underground metro
stations (DMRC use case).

This document is the **source of truth** for the project. Update it in the
same commit as any change to architecture, schema, commands or decisions.

---

## 0. Non-negotiables

1. **Terminal only.** `tbfmon` is a CLI. Everything runs through the `tbfmon`
   command in a plain terminal and must work over SSH with no display.
   - No web server, browser UI, GUI or Electron.
   - No Flask / FastAPI / Django / Streamlit / Gradio / Dash (or similar).
   - If a feature seems to need a web UI: stop and ask the project owner.
2. **Fully local.** No cloud services, no network calls at runtime. SQLite file
   storage.
3. **Ground truth isolation.** The simulator's `ground_truth.csv` is written to a
   separate directory and must never be read by ingestion, validation or the
   views. Enforced by `tests/test_ground_truth_isolation.py` (static: which
   modules may mention it; dynamic: ingest, validate and the fleet/fan views run
   with every ground-truth file access rigged to fail) and by `CsvSource`
   refusing it.
4. **Placeholder limits are marked.** Every permissible limit shipped in
   `limits.yaml` is a placeholder until replaced with OEM values; each carries
   `placeholder: true` and a `REPLACE WITH OEM VALUES` comment. Commands surface
   a warning while any placeholder remains.
5. **Deterministic simulation.** Same `--seed` and arguments ⇒ byte-identical
   output files. The simulator never uses wall-clock time.
6. **`--json` on every command** for machine-readable output (stdout only;
   human messages go to stderr in JSON mode).
7. **UI-free data layer.** Only `cli.py` and `render.py` import Typer or Rich
   (enforced by `tests/test_architecture.py`), so the data layer can be reused
   by another front end without changes.

## 1. Problem summary

- TBFs of different capacities sit idle and only run during mock drills and
  tests. They are safety-critical (smoke control) but accumulate very few
  run-hours.
- OEM maintenance is **time-based** preventive maintenance (PM): fixed-interval
  tasks, many of which require **work at height** (platforms/scaffolding in
  live tunnels during short night blocks). This is expensive in manpower and
  carries safety risk, while doing little for fans that barely run.
- Goal: decide maintenance from each fan's **actual condition**, using
  - readings from test runs (vibration, temperatures, currents, airflow, IR, …),
  - permissible limits (OEM / standards),
  - past maintenance records,
  - historical failures and the symptoms that preceded them.
- Data reality: sparse and irregular (≈4–12 runs per fan per year), short runs
  (thermal steady state often not reached), portable instruments (not every
  parameter every run), manual transcription errors.

**Termino Lite (this version)** covers the foundation: data model, storage,
synthetic data, ingestion, data-quality validation and the limit-based
inspection commands. The condition-based analytics engine (drift, anomaly
detection, fault diagnosis, health index, maintenance recommendations) is the
next phase; see §8.


## 2. Architecture

```
  CSV exports · simulator · (MQTT/Modbus stubs)
                     │ sources/ → ingest.py (sanitise → validate → idempotent upsert)
                     ▼
            ┌──────────────── SQLite (tbfmon.db) ────────────────┐
            │ fans · PM tasks · runs · maintenance · failures ·  │
            │ data-quality issues · ingest batches               │
            └────────────────────────┬───────────────────────────┘
                                     ▼
   validate.py   data-quality and consistency checks, suspect-value rules
   limits.py     every reading vs limits.yaml → OK / WARN / ALARM
   pm.py         OEM PM due / overdue
   views.py      plain-data views behind `fleet list` / `fan show`
                                     ▼
   cli.py + render.py   Typer + Rich  ──►  terminal tables · sparklines · --json

   simulate/ ──► CSV bundle + ground_truth.csv (kept separate, never read)
```

Package layout:

```
src/tbfmon/
  cli.py              Typer app, all commands
  render.py           Rich tables, sparklines, JSON emit
  paths.py            workspace ("home") layout
  models.py           pydantic models + enums + measurement catalogue
  db.py               SQLite schema, connection, upserts, queries
  limits.py           limits.yaml schema, loading, evaluation
  pm.py               OEM PM due/overdue computation
  validate.py         data-quality and consistency checks; suspect-value rules
  views.py            plain-data views behind `fleet list` / `fan show`
  ingest.py           source → sanitize → validate → upsert
  sources/            Source protocol + CsvSource, SimulatorSource, MQTT/Modbus stubs
  simulate/           synthetic fleet generator (fleet, faults, generator)
  services/setup.py   workspace creation and simulated-fleet loading (`init`, `simulate`)
  __main__.py         `python -m tbfmon`
  data/limits.yaml    default (placeholder) limits copied by `tbfmon init`
```

### Workspace ("home")

`--home PATH` (global option) or `TBFMON_HOME`, default `./tbfmon_data`:

```
tbfmon_data/
  tbfmon.db                       SQLite database
  limits.yaml                     editable copy of permissible limits
  sim/                            simulator CSV bundle (ingestible)
    fans.csv pm_schedule.csv test_runs.csv maintenance.csv failures.csv
  ground_truth/ground_truth.csv   kept separate — never ingested
```

`tbfmon_data/` is git-ignored.

## 3. Data model

All models are pydantic v2 (`models.py`). Timestamps are naive local time
(IST); dates are ISO `YYYY-MM-DD`.

### Fan
| field | type | notes |
|---|---|---|
| fan_id | str | PK, e.g. `TBF-001` |
| station | str | |
| line | str? | metro line |
| tunnel_section | str | e.g. `UP Rajiv Chowk–Patel Chowk` |
| capacity_class | str | key into `limits.yaml` (`C30`, `C45`, `C55`) |
| rated_power_kw, rated_airflow_m3s, rated_diff_pressure_pa, rated_rpm, rated_voltage_v, rated_current_a | float | nameplate |
| oem_model | str | |
| install_date | date | |
| pm_schedule | list[PMTask] | OEM time-based PM |

**PMTask**: `task`, `interval_months?`, `interval_run_hours?` (at least one
required), `needs_work_at_height: bool`.

### TestRun
Key: `(fan_id, timestamp, run_type)`. `run_type ∈ {mock_drill, scheduled_test,
commissioning}`, `duration_min > 0`. All measurements nullable:

| field | unit | meaning |
|---|---|---|
| vib_de_rms_mms, vib_nde_rms_mms | mm/s | overall velocity RMS, drive/non-drive end bearing |
| vib_de_1x_mms, vib_de_2x_mms, vib_nde_1x_mms, vib_nde_2x_mms | mm/s | 1× and 2× running-speed amplitudes |
| temp_bearing_de_c, temp_bearing_nde_c | °C | end-of-run bearing temps |
| temp_winding_c | °C | winding (RTD/PT100) |
| current_l1_a, current_l2_a, current_l3_a | A | phase currents |
| current_imbalance_pct | % | max dev from mean / mean; computed if absent |
| voltage_v | V | line voltage |
| rpm | rpm | |
| airflow_m3s | m³/s | |
| diff_pressure_pa | Pa | |
| noise_dba | dB(A) | at 1 m |
| insulation_resistance_mohm | MΩ | 500 V DC, 1 min |
| runup_time_s, coastdown_time_s | s | |
| inrush_peak_a | A | |
| ambient_temp_c | °C | tunnel ambient |

The measurement catalogue (`models.MEASUREMENTS`) holds unit, label and the
**sensor range** for each field. Values outside sensor range are physically
impossible for the instrument and are nulled at ingest (see decisions).

### MaintenanceRecord
Key: `(fan_id, date, task)`. `type ∈ {preventive, corrective}`,
`parts_replaced: list[str]`, `findings: str`, `man_hours?`,
`work_at_height?`.

### FailureRecord
Key: `(fan_id, date, failure_mode)`. `failure_mode ∈ {bearing_wear,
impeller_imbalance, misalignment_looseness, insulation_degradation,
airflow_restriction, lubrication_loss, other}`, `symptoms: list[Symptom]`
where `Symptom = {parameter, direction ∈ {increase, decrease, erratic}, note}`,
`root_cause`, `fix`, `downtime_hours?`.

CSV encoding of lists: `parts_replaced` is `;`-separated; `symptoms` is
`param:direction:note` items separated by `|`.

### SQLite schema (`db.py`, schema_version 2)

```
meta(key PK, value)
fan(fan_id PK, station, line, tunnel_section, capacity_class, rated_*…,
    oem_model, install_date, source, record_hash, ingested_at)
pm_task(fan_id FK, task, interval_months, interval_run_hours,
    needs_work_at_height, source, record_hash, PK(fan_id, task))
test_run(run_id PK, fan_id FK, ts, run_type, duration_min, <measurements…>,
    source, record_hash, ingested_at, UNIQUE(fan_id, ts, run_type))
maintenance(id PK, fan_id FK, date, task, type, parts_replaced JSON, findings,
    man_hours, work_at_height, source, record_hash, ingested_at,
    UNIQUE(fan_id, date, task))
failure(id PK, fan_id FK, date, failure_mode, symptoms JSON, root_cause, fix,
    downtime_hours, source, record_hash, ingested_at,
    UNIQUE(fan_id, date, failure_mode))
data_quality_issue(id PK, entity, entity_key, field, raw_value, issue, action,
    source, detected_at, UNIQUE(entity, entity_key, field, issue))
ingest_batch(id PK, source, started_at, finished_at, inserted, updated,
    unchanged, rejected)
```
All tables use `CREATE TABLE IF NOT EXISTS`, so `init` is safe to re-run.

### limits.yaml

```yaml
version: 1
status: PLACEHOLDER
defaults:                 # every class inherits these
  <parameter>:
    basis: absolute | pct_of_rated | rise_over_ambient
    warn_high / alarm_high / warn_low / alarm_low: float   # any subset
    unit, note
    placeholder: true     # REPLACE WITH OEM VALUES
classes:
  C30: {description, overrides: {<parameter>: {...}}}
```

`pct_of_rated` compares against the fan's nameplate (current → rated_current_a,
voltage → rated_voltage_v, rpm, airflow, differential pressure, inrush →
rated_current_a). `rise_over_ambient` compares `value − ambient_temp_c`.
Status per value: `OK | WARN | ALARM | NA`.

## 4. Synthetic data (`tbfmon simulate`)

No real DMRC data exists. The generator (`simulate/`) is seeded
(`numpy.random.Generator(PCG64(seed))`), starts at a fixed `--start` date
(default 2023-01-01) and produces:

- **Fleet**: stations/sections on underground corridors, three capacity
  classes (C30/C45/C55), fictional OEM models, install dates 2006–2021,
  per-model OEM PM schedules (several tasks need work at height).
- **Per-fan baselines**: vibration components (1×, 2×, broadband), load
  factor, thermal rises, airflow factor, IR level, noise, run-up/coast-down,
  inrush multiplier (soft-starter vs DOL), small phase asymmetry.
- **Sampling**: station-wide mock drills (1–3/yr) plus per-fan scheduled tests
  (Poisson), totalling ≈4–12 runs/fan/yr at night; commissioning runs after
  corrective repairs. Durations: drills 20–60 min, tests 5–30 min.
- **Physics-lite readings**: overall RMS = √(1ײ + 2ײ + broadband²);
  temperatures follow `ambient + rise·(1 − e^(−t/τ))` so short runs are cooler;
  seasonal tunnel ambient; slip-dependent rpm; airflow ∝ rpm; IR depends on
  humidity season; per-parameter instrument availability (airflow, noise, IR
  not measured every run).
- **Faults** (rate ∝ fan age), severity `s = ((t−onset)/(fail−onset))^k`:

| mode | signature |
|---|---|
| bearing_wear | broadband RMS ↑ at affected bearing (1×/2× barely), late bearing temp ↑, noise ↑, coast-down ↓ |
| impeller_imbalance | 1× ↑ at both bearings (DE > NDE), RMS follows 1×, slight current ↑ |
| misalignment_looseness | 2× ↑ (DE), 1× ↑, run-to-run scatter ↑, DE temp ↑ |
| insulation_degradation | IR ↓ exponentially, late current imbalance ↑, winding temp ↑ |
| airflow_restriction | airflow ↓, ΔP ↑, current ↓, noise ↑ |
| lubrication_loss | bearing temp ↑↑, late vibration ↑, noise ↑, coast-down ↓, current ↑ |

  Plus rare sudden `other` failures (control circuit / cable) with no precursor.
- **Failure discovery**: a fault that has reached `s ≥ 1` is discovered at the
  fan's next run (trip: short duration, extreme values). A failure record lists
  the symptoms computed from the actual pre-failure runs vs the fan's
  baseline. Corrective maintenance follows 1–10 days later, then a
  commissioning run; the repaired component gets a fresh baseline.
- **PM interaction**: OEM PM is done with imperfect compliance (skips, delays).
  Some tasks resolve early faults (regreasing → lubrication loss, cleaning →
  airflow restriction, torque check → looseness) and leave findings text.
- **Data defects**: 1.5 % of values glitched (spikes ×3–10, stuck zero,
  sentinel −999/9999, °F instead of °C), 1.5 % extra missing values, ~0.5 %
  duplicated run rows.
- **Ground truth**: `ground_truth.csv` (fan_id, fault_id, fault_mode, location,
  onset_date, failure_date, end_date, outcome ∈ {failed, resolved_by_pm,
  ongoing}) in `<home>/ground_truth/`, outside the ingestible bundle. Nothing in
  Termino Lite reads it; it is kept for the next phase's evaluator (§8).

## 5. Ingestion

`sources.Source` protocol: `name` and `records() -> Iterator[RawRecord]` where
`RawRecord = (kind, data: dict, origin: str)` and `kind ∈ {fan, pm_task,
test_run, maintenance, failure}`.

- `CsvSource(path)`: a bundle directory (files processed in dependency order)
  or a single file (kind from filename or `--kind`). Refuses any
  `ground_truth*` file or path.
- `SimulatorSource(dataset)`: in-memory records straight from the generator.
- `MqttSource`, `ModbusSource`: documented stubs (topic / register maps, run
  aggregation contract). Raise `NotImplementedError`.

Pipeline per record: **sanitize** (null out-of-sensor-range values, log a
`data_quality_issue`) → **pydantic validate** (reject row on schema error,
unknown fan, bad date; logged with action `rejected`) → **upsert** keyed by
natural key. A record hash (normalised values, rounded to 6 dp, provenance
excluded) decides `inserted | updated | unchanged`. Re-ingesting the same data
from any source is a no-op. Each run is logged in `ingest_batch`.

## 6. Commands

Global: `tbfmon [--home PATH] <command> … [--json]` (also `python -m tbfmon`).

| command | purpose | exit |
|---|---|---|
| `init [--force]` | create home, DB schema and default `limits.yaml` (kept unless `--force`) | 0 |
| `simulate --fans 40 --years 3 --seed 42 [--start D] [--no-ingest]` | synthetic CSV bundle + ground truth, then ingest | 0 |
| `ingest <path> [--kind K]` | ingest a CSV bundle dir or one CSV (idempotent) | 0 |
| `validate [--examples N] [--as-of D]` | data-quality checks (below) | 0 PASS · 1 WARN · 2 FAIL |
| `fleet list [--station S] [--class C] [--status S] [--sort id\|status] [--as-of D]` | limit-condition view per fan (§7.9) incl. OEM PM overdue | worst condition |
| `fan show <id> [--runs N] [--as-of D]` | nameplate, readings vs limits with sparklines, PM due, history | condition |

**Exit codes** (for cron / SCADA scripts): `0` ok · `1` warn · `2` alarm ·
`3` error (bad input, unknown fan, missing file, not initialised —
including Click usage errors, whose default of 2 would read as ALARM).
Condition maps OK/NA → 0, WARN → 1, ALARM → 2. With `--json`, errors are
emitted as `{"error": …}` on stdout.

`validate` checks: `limits_file` (valid; placeholders ⇒ WARN), `capacity_classes`
(FAIL if a fan's class has no limits), `fans_without_runs`,
`fans_without_pm_schedule`, `ingest_nulled_values`, `ingest_rejected_rows`,
`temporal_consistency` (before install / in the future), `stuck_zero_values`,
`isolated_spikes` (with hint: spike, decimal shift ×10, looks like °F, stuck
zero, dropout), `physical_consistency` (RMS < 1×/2× component, bearing/winding
below ambient, rpm above synchronous, stated vs computed current imbalance),
`fans_not_run_recently` (>120 days), `failures_without_symptoms`; plus a
missing-value table per parameter.

Output adapts to terminal width (narrow SSH terminals drop low-priority
columns rather than cropping); colour is disabled automatically when piped.

## 7. Key decisions

1. **SQLite + natural keys + content hash** for idempotent, source-agnostic
   ingestion (CSV and simulator produce identical hashes for identical data).
2. **Two-tier bad-value handling.** Physically impossible values (outside
   sensor range, sentinels) are nulled at ingest and logged; the rest of the
   row is kept (sparse data is too valuable to drop). Plausible-but-suspect
   values (isolated spikes, °F slips) are kept and *flagged* by `validate`, so
   no genuine degradation is silently discarded.
3. **Isolated-spike test, not global outlier test.** A value is suspect only if
   it deviates strongly from both neighbouring runs while the neighbours agree.
   Persistent deviation is degradation, not a glitch.
4. **Limits in YAML per capacity class** with inheritance from `defaults`,
   three bases (absolute / % of rated / rise over ambient), placeholder flags.
5. **Short-run thermal effect is modelled** (temperature depends on run
   duration). Analytics must normalise by duration — noted for the next phase (§8).
6. **Run-hours are derived** from recorded test-run durations only (pre-data
   history unknown).
7. **Time zone**: naive local time (IST) throughout.
8. **Units** are fixed per field (see §3); ingestion does not convert units.
9. **Condition = latest *trustworthy* readings vs limits.** Per parameter, the
   most recent value within the fan's last 3 runs is used. Values are
   **SUSPECT** (shown, but excluded from the condition verdict) when they are
   an isolated spike (disagree with the left *and* right neighbours
   separately), an unconfirmed newest jump (one-sided test vs the previous
   runs — re-measure to confirm), a phase current >30 % from the other two
   phases in the same run, or an exact zero where a running fan cannot read
   zero. Temperatures are tested as duration-normalised rise over ambient
   (generic τ = 25 min), IR in log space. Gradual trends are never suspect.
10. **Repairs reset history.** Corrective maintenance dates split a fan's
    history for the suspect test: a repair legitimately changes the baseline
    (e.g. IR after a rewind).
11. **Reference date (`--as-of`)** defaults to the latest date in the data,
    not the wall clock, so due/overdue and staleness are meaningful for
    historical or simulated data; `--as-of today` for live use.
12. **OEM PM status** is shown as the time-based baseline CBM must beat:
    per task last done, next due by calendar and by run-hours, OVERDUE /
    DUE SOON / NO RECORD, and whether it needs work at height.
13. **Corroborated jumps are real.** A sudden change that the causal suspect
    test would hold back is accepted at once if a physically related parameter
    moved notably in the same run (vibration ↔ bearing temperature/noise, IR ↔
    current imbalance/winding temperature, airflow ↔ ΔP/current). Glitches hit
    single values independently; faults move several. Without this, a genuine
    ALARM seen during a drill would be excluded from the verdict.


## 8. Next phase (roadmap)

Termino Lite tells you when a reading is **already** over a limit. The next
phase adds early warning and maintenance decisions on top of the same database:

- Per-fan robust baselines, drift detection (EWMA, CUSUM) and peer comparison.
- Anomaly detection (Isolation Forest) on healthy runs.
- Fault-mode diagnosis from vibration/thermal signatures, plus a supervised model.
- Trend and time-to-alarm estimates; a Health Index per fan.
- Condition-based maintenance recommendations (defer / inspect / repair) and
  man-hour savings vs the OEM schedule.
- Mock-drill workflow, live folder watch, persisted alerts and report files.
- Evaluation against the simulator's ground truth (the only reader of it).
- Replace placeholder limits with OEM values.
