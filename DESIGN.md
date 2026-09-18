# tbfmon — Design

Condition-based monitoring and maintenance for Tunnel Booster Fans (TBF) in
underground metro stations (DMRC use case).

This document is the **source of truth** for the project. Update it in the
same commit as any change to architecture, schema, commands or decisions.

---

## 0. Non-negotiables

1. **Terminal only.** `tbfmon` is a CLI. Everything runs through the `tbfmon`
   command in a plain terminal and must work over SSH with no display.
   - No web server, browser UI, GUI or Electron.
   - No Flask / FastAPI / Django / Streamlit / Gradio / Dash (or similar).
   - HTML is allowed **only** as an exported report *file*, never as a running app.
   - If a feature seems to need a web UI: stop and ask the project owner.
2. **Fully local.** No cloud services, no network calls at runtime. SQLite file
   storage.
3. **Ground truth isolation.** The simulator's `ground_truth.csv` is written to a
   separate directory and must never be read by ingestion or any analytics code.
   Only `tbfmon.evaluation` (`tbfmon evaluate`) may read it. Enforced by
   `tests/test_ground_truth_isolation.py` (static: which modules may mention it,
   the engine never imports the evaluator; dynamic: the whole engine runs with
   every ground-truth file access rigged to fail) and by `CsvSource` refusing it.
4. **Placeholder limits are marked.** Every permissible limit shipped in
   `limits.yaml` is a placeholder until replaced with OEM values; each carries
   `placeholder: true` and a `REPLACE WITH OEM VALUES` comment. Commands surface
   a warning while any placeholder remains.
5. **Deterministic simulation.** Same `--seed` and arguments ⇒ byte-identical
   output files. The simulator never uses wall-clock time.
6. **`--json` on every command** for machine-readable output (stdout only;
   human messages go to stderr in JSON mode).
7. **Explainable analytics.** Every engine output carries the evidence a
   maintenance engineer needs: parameter, value, baseline, limit, z-score,
   reason codes. No single opaque model decides anything.
8. **Evaluation results are labelled "SIMULATED DATA"** everywhere they appear.

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

Phase 1 (foundation): data model, storage, synthetic data, ingestion,
validation and inspection commands. Phase 2 (this document §9–§10): the
layered analytics engine, recommender and evaluation.

## 2. Architecture

```
            ┌───────────────────────── sources/ ─────────────────────────┐
            │ CsvSource   SimulatorSource   MqttSource*   ModbusSource*  │  (*stubs)
            └──────────────────────────────┬─────────────────────────────┘
                                           │ RawRecord(kind, data, origin)
                                           ▼
                         ingest.py  ── sanitize (sensor ranges) ──► data_quality_issue
                                           │ pydantic models (models.py)
                                           ▼
                         db.py  (SQLite, idempotent upsert by natural key + hash)
                                           │
            ┌──────────────────────────────┼──────────────────────────────┐
            ▼                              ▼                              ▼
     validate.py                 limits.py (limits.yaml)          pm.py (OEM PM due)
            └──────────────────────────────┬──────────────────────────────┘
                                           ▼
                        cli.py (Typer) + render.py (Rich tables / --json)
                                           │
                 engine/ (layers 0–8, reads DB only) ──► assess / recommend
                                           │
                 evaluation/ (only reader of ground truth) ──► evaluate

  simulate/ (generator) ──► CSV bundle  <home>/sim/*.csv
                        └─► <home>/ground_truth/ground_truth.csv   (evaluator only)
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
  engine/             analytics engine, layers 0–8 (§9) — never reads ground truth
  evaluation/         `tbfmon evaluate` — the ONLY reader of ground_truth.csv (§10)
  cli_engine.py       assess / recommend / evaluate commands
  data/limits.yaml    default (placeholder) limits copied by `tbfmon init`
  data/engine.yaml    engine thresholds, weights and cost assumptions (copied by `init`)
```

### Workspace ("home")

`--home PATH` (global option) or `TBFMON_HOME`, default `./tbfmon_data`:

```
tbfmon_data/
  tbfmon.db                       SQLite database
  limits.yaml                     editable copy of permissible limits
  engine.yaml                     editable engine configuration (§9)
  sim/                            simulator CSV bundle (ingestible)
    fans.csv pm_schedule.csv test_runs.csv maintenance.csv failures.csv
  ground_truth/ground_truth.csv   evaluator only — never ingested
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

### SQLite schema (`db.py`, schema_version 1)

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
  ongoing}) in `<home>/ground_truth/`, outside the ingestible bundle.

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

Global: `tbfmon [--home PATH] <command> … [--json]`

| command | purpose |
|---|---|
| `init [--force]` | create home, DB schema, copy default `limits.yaml` (kept if present unless `--force`) |
| `simulate --fans 40 --years 3 --seed 42 [--start DATE] [--no-ingest]` | generate synthetic CSV bundle + ground truth, then ingest via SimulatorSource (requires `init`) |
| `ingest <path> [--kind K]` | ingest a CSV bundle dir or single CSV (idempotent) |
| `validate [--examples N] [--fail-on-warn] [--as-of D]` | checks below; exit 1 on FAIL (or WARN with `--fail-on-warn`) |
| `fleet list [--station S] [--class C] [--status S] [--sort id\|status] [--as-of D]` | one row per fan: runs, run-hours, last run, condition, flagged and suspect parameters, OEM PM overdue (and at height), failures |
| `fan show <id> [--runs N] [--as-of D]` | nameplate, latest readings vs limits with reference and trend sparklines, OEM PM due status, maintenance and failure history |
| `assess [--fan ID] [--as-of D]` | fleet Health Index table (lowest first), or one fan fully explained: HI penalties, reasons, fault modes (rules/model/combined with reasons), limit check, drift/peer table, anomaly contributors, trends with time-to-ALARM on three axes, recommendation |
| `recommend [--all] [--as-of D]` | per-fan action, by-date, tasks, work-at-height, reason codes; OEM vs CBM task/man-hour/height-hour comparison over the horizon, with assumptions |
| `evaluate [--as-of D]` | SIMULATED DATA: detection recall, lead time, false alarms per fan-year, alert precision, per-mode identification; vs threshold-only and OEM baselines (§10) |

Exit codes: 0 ok, 1 validation failure / unknown fan, 2 usage or setup error
(e.g. not initialised, ground truth refused). With `--json`, errors are also
emitted as `{"error": ...}` on stdout.

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
   duration). Analytics must normalise by duration — noted for the ML phase.
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

## 8. Phase 2 overview

The analytics engine (§9) sits on top of the foundation and reads only the
database; `tbfmon evaluate` (§10) scores it against simulator ground truth.
Open work is in §11.

## 9. Analytics engine (`tbfmon/engine/`)

### Why layers, not one model
Fans only run during drills and tests: ~4–12 runs per fan per year, most
parameters not measured every run, and only a handful of recorded failures
per mode. A single supervised model trained on that would overfit and could
not be explained. So the engine is a stack of simple, individually
checkable layers. Each one is useful on its own, and the transparent layers
(limits, baselines, rules) carry the decisions. ML is used where it adds
something (multivariate anomaly, extra mode evidence) and is kept from
overruling what the engineer can see.

All thresholds, weights and cost assumptions are in `engine.yaml`, copied to
the workspace by `init`. Randomness is fixed by `seed` (IsolationForest,
RandomForest). The engine reads only the database: runs, maintenance,
failures, PM schedule, limits.

| layer | module | what it does | output an engineer sees |
|---|---|---|---|
| 0 | `clean.py` | causal suspect screening: stuck zero, phase current >30 % from the others, one-sided jump vs the previous accepted values (accepted once the next run confirms it) | `suspect_values` per run; suspect values never drive a verdict |
| 1 | `limit_check.py` | every reading vs `limits.yaml` | parameter, value, basis, warn/alarm on the relevant side, **% deviation from WARN** (>0 = beyond), OK/WARN/ALARM/SUSPECT |
| 2 | `features.py` | normalised features: thermal rise over ambient **duration-normalised** to steady state (τ = 25 min; runs <0.2 τ ignored); IR **corrected to 40 °C**, log10; airflow ∝ rpm and ΔP ∝ rpm² (**fan laws**) as % of rating; current % rated; vibration **broadband** √(RMS² − 1ײ − 2ײ); run-up, coast-down, inrush/rated (**start/stop behaviour**); **deltas vs previous run** | feature values + Δ |
| 3 | `baseline.py` | per-fan baseline = median/MAD of the first 5 values of the segment (≥3 needed); robust z with per-feature noise floors; bad-direction z; **EWMA** (λ 0.3) and one-sided **CUSUM** (k 0.5, h 5); **peer z** vs the other fans of the capacity class (leave-one-out, trailing year, month-start snapshots, causal); sparse parameters carried forward ≤180 days | value, baseline, z, EWMA, CUSUM, drift flag, peer z |
| 4 | `anomaly.py` | **Isolation Forest per capacity class** on the fan's z-vector (missing ⇒ 0), trained on healthy runs only (below); score = percentile among healthy training runs | percentile, anomalous if ≥0.98, **top contributors by occlusion** |
| 5a | `rules.py` | **signature rules**: 1× ↑ & dominant → imbalance; 2× or 2×/1× ↑, scatter → misalignment/looseness; broadband ↑ (+temp, noise, coast-down) → bearing wear per bearing; bearing rise ↑ → lubrication loss; IR@40 ↓ (+ current imbalance, winding) → insulation, falling back to current imbalance at 0.7 weight when IR is unmeasured; airflow ↓ / ΔP ↑ (+ current ↓) → airflow restriction | mode, location, confidence, **reasons with values and z** |
| 5b | `supervised.py` | **RandomForest** (300 trees, depth ≤6, leaf ≥3, class-balanced, native NaN) on z + peer z + scatter; labels from FailureRecords: 1–180 d before a failure = its mode, no failure within a year = healthy, otherwise excluded; **GroupKFold by fan** | class probabilities; warnings for modes with <3 recorded failures |
| — | `pipeline.py` | combined mode confidence = 0.6·rule + 0.4·model; for modes where the model is unreliable, max(rule, blend), so a weak model can add evidence but never veto a rule | top-3 modes |
| 6 | `trend.py` | **Theil–Sen** slope (90 % CI) of vib RMS DE/NDE, bearing rise DE/NDE, IR@40, airflow %, current imbalance, coast-down over the last ≤10 runs of the segment vs **calendar days, run-hours and starts**; time-to-ALARM from the class limits with CI; "insufficient data" if <4 runs or <60 days | status, slope, ETA (fast–slow CI) per axis |
| 7 | `health.py` | **Health Index** = 100 − Σ weight·component: limits 30 (WARN 0.5, ALARM 1), drift 20 (worst bad-dir z/6, +0.25 if EWMA/CUSUM), anomaly 15 ((p−0.5)/0.5), fault mode 15 (top confidence), trend 15 (ETA <90 d 1, <180 d 0.6, <365 d 0.3), peers 5 (worst peer z/6). Status ≥80 Healthy, ≥60 Watch, ≥40 Degraded, else Critical; any ALARM ⇒ Critical, any WARN ⇒ at best Watch | HI, status, per-component penalties, reason codes |
| 8 | `recommend.py` | Critical → **urgent corrective** (7 d); Degraded, or Watch with a mode ≥0.5 → **targeted inspection of X** (30/60 d, sooner if the trend ETA is close); Healthy with baseline and recent data → **defer OEM PM** for deferrable tasks whose related modes are all <0.5 (≤2× interval since last done); else **no action** (OEM continues). Savings over 365 d: OEM task occurrences vs CBM plan × man-hours (10 h at height, 2 h ground, 8 h inspection, 16 h corrective) | action, by-date, tasks, needs_work_at_height, reason codes, deferred tasks, OEM vs CBM hours |

**Healthy runs** (for layer 4) are defined from records only: not within
180 d before a recorded failure, 90 d before a corrective repair, or 60 d
before a PM whose findings contain abnormal keywords (`labels` in engine.yaml).

### Key engine decisions and trade-offs
1. **Causal everywhere per fan.** Every per-fan statistic at run *i* uses
   only runs ≤ *i*. This costs some accuracy: the two-sided spike test in
   `validate` is better, but it looks at the next run. In exchange,
   `evaluate` is an honest point-in-time replay.
2. **Repairs reset only what they touch.** Segments are per feature group
   (`features.MODE_RESETS`): a bearing replacement resets vibration, bearing
   temperature, noise and start/stop, but not airflow or insulation. A repair
   with no failure record resets everything. With a full reset, fans were
   blind for three runs after every repair, and on seed 42 that missed a
   later imbalance failure.
3. **Sparse parameters are carried forward** (≤180 d) for rules and HI. IR is
   measured on ~35 % of runs, and without this the insulation rule almost
   never fired.
4. **Rules first, model second.** With 0–5 failures per mode the RandomForest
   is unreliable. It is shown and blended, but it cannot dilute a rule for a
   mode it has too little data on. On a 5-year/112-fan simulation (97
   failures) the model becomes as good as the rules at naming modes; the
   blend is designed to shift weight as failures accumulate.
5. **Isolation Forest looks for combinations.** In ~21 dimensions, a single
   extreme feature is diluted (a lone 12σ value scored only the 90th
   percentile out of fold). Single-parameter deviations are caught by limits
   and drift, so IF is not asked to.
6. **Peers as a fallback (at 70 % weight) and 5 % of HI.** They catch faults
   that were already present when data started (the baseline contains the
   fault), but fan-to-fan offsets make them noisy.
7. **Calendar time drives time-to-ALARM.** Idle fans degrade mainly with time
   (grease ageing, moisture, dust); run-hours/start trends are reported too.
8. **Safety bias in recommendations.** Nothing is deferred without an
   established baseline or with data older than 120 days. IR tests and
   control/starter checks are never deferred. Prevented-failure benefits are
   not counted in savings.
9. **No axial vibration channel** exists in the data model, so misalignment
   relies on 2× and the 2×/1× ratio only.

## 10. Evaluation (`tbfmon evaluate`) — SIMULATED DATA

Method: the engine runs in `oof` mode. Per-fan layers are causal. Isolation
Forest and RandomForest score each fan with models trained only on other
fans (GroupKFold by fan). Those other fans' *full* histories are used, which
is a mild cross-fan temporal leak, accepted and documented. Per run the
recommended action is replayed.
* **Alert** = action is targeted inspection or urgent corrective (a
  "sensitive" variant alerts on any non-Healthy status).
* **Detected failure** = an alert on a run on/after onset and strictly
  before the failure date. **Lead time** = failure − first such alert.
* **False alarm** = an alert event (consecutive alerting runs) that overlaps
  no fault episode [onset − 30 d, end]; rate per fan-year.
* **Fault-mode identification**: per-class precision/recall of the named
  mode at the first and at the last alert of each detected episode, for
  rules, model and combined.
* **Baselines**: threshold-only (any raw reading WARN/ALARM; ALARM-only
  variant; no suspect screening) and the OEM time-based schedule as
  simulated (episodes resolved by PM, PM hours, share of visits that found
  nothing).

### Results at this commit (SIMULATED DATA)

The rules were developed while looking at seed 42. Seed 7 and the large
fleet were not used for development.

| dataset | system | failures caught | median lead | false alarms / fan-yr | alert precision |
|---|---|---|---|---|---|
| seed 42, 40 fans × 3 y | CBM engine | 8/10 | 195 d | 0.11 | 0.52 |
| | CBM sensitive (Watch+) | 9/10 | 225 d | 0.41 | 0.34 |
| | threshold WARN/ALARM | 7/10 | 87 d | 0.62 | 0.23 |
| | threshold ALARM | 5/10 | 91 d | 0.52 | 0.17 |
| | OEM PM (simulated) | 11/21 episodes caught before failure | — | 7.7 visits, 97 % found nothing | — |
| seed 7, 40 × 3 (unseen) | CBM engine | 11/19 | 225 d | 0.09 | 0.68 |
| | threshold WARN/ALARM | 11/19 | 109 d | 0.62 | 0.27 |
| | OEM PM | 6/25 episodes | — | 8.0 visits | — |
| seed 2024, 112 × 5 (unseen) | CBM engine | 80/97 | 152 d | 0.08 | 0.74 |
| | CBM sensitive | 90/97 | 180 d | 0.50 | 0.38 |
| | threshold WARN/ALARM | 70/97 | 152 d | 0.52 | 0.36 |
| | OEM PM | 79/176 episodes | — | 7.9 visits, 96 % found nothing | — |

Mode named at the last alert and correct: 92 % (seed 42), 100 % of 11
named (seed 7), 83 % (112 fans; rules 79 %, model 84 %). Estimated OEM PM
effort avoided next year by the recommender: 26–40 man-hours per fan-year
(all of it work at height), against ~2 h/fan-yr of triggered inspections.

### Where it performs badly (be explicit)
* **Recall is not reliably better than thresholds.** On seed 7 it is equal
  (11/19). The consistent gains are **~6× fewer false alarms** and, on
  small fleets, earlier warning.
* **Lubrication loss** (7/12 caught on the large fleet, mode recall 0.5) and
  **airflow restriction** (5/7, where thresholds got 6/7; mode recall 0.36)
  are the weakest modes. Lubrication loss develops in weeks to months
  between sparse runs. Airflow is measured on <50 % of runs.
* **Sparse sampling is the hard limit.** Failures preceded by ≤2 runs, or by
  months without a run, cannot be caught (e.g. TBF-019 on seed 42: no run
  in the 6 months before failure).
* **Sudden control-circuit failures** (`other`) have no precursors and are
  never detected. By design they are excluded from recall.
* **Early in an episode the mode is often not named** (first-alert naming
  8–10 of 13 on seed 42). Identification improves as the fault develops.
* **Run-level supervised precision is low** (0.09–0.49 per mode on the
  large fleet). The RandomForest is useful only as episode-level evidence
  and only with many recorded failures.
* **Faults present before data starts** end up in the fan's baseline; only
  peers (noisy) can reveal them.
* **Everything is tuned on synthetic data** whose physics the same author
  wrote. Real signatures, noise and failure rates will differ, and all
  limits are placeholders.

## 11. Roadmap

- Replace placeholder limits with OEM values; review engine.yaml thresholds on site data.
- Weak labels from PM findings (e.g. "grease dry" → lubrication) to enlarge training sets.
- Measurement-policy advice: which parameters to add to every drill (airflow, IR) for the weakest modes.
- Axial vibration channel and envelope/spectral features when instruments allow.
- Exported HTML/Markdown report file (file only, never a running app).

