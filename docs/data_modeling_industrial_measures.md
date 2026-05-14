# Data modeling for industrial measurements in Fabric Eventhouse

> Companion documents: [`concepts.md`](concepts.md) (architecture
> overview) and [`anomaly_detection_fabric_kql.md`](anomaly_detection_fabric_kql.md)
> (KQL cookbook).

## Long, Wide, or Hybrid — which to choose and why

This document compares the three main data-modeling strategies for process measurements coming from industrial machines, with a focus on usage in Fabric Eventhouse for anomaly detection, dashboards, and historical analysis. The choice is not purely aesthetic: it has measurable impact on query performance, ML pipeline complexity, maintenance cost, and the ability to evolve the schema over time.

---

## 1. Context

In industrial settings, process data typically arrives as a **stream of point samples**: each record is a numeric value associated with a timestamp, a machine identifier, and a signal identifier (sensor, OPC-UA tag, process variable). Typical characteristics:

- High volumes: from tens to thousands of samples per second per plant
- Heterogeneity: machines of different kinds (presses, compressors, CNCs, robots, ...) with physically non-comparable signals
- Schema potentially evolving: new sensors added over time, new machines installed, new plant types
- Multiple consumers: anomaly detection, real-time dashboards, predictive maintenance, production reporting, integration with MES/ERP

The question "how to model this data" decomposes along two independent axes:

1. **Record granularity**: one row per point measurement (*long*) or one row per sampling instant with a value for each signal (*wide*)?
2. **Physical segregation**: a single table holding everything, or separate tables per machine type?

Combining the two axes yields the three practical architectures described below.

---

## 2. Type A — Single long table with classification columns

### Structure

```kusto
.create table measures_enriched (
    ts: datetime,
    machine_id: string,        // 'press_01', 'comp_03', 'cnc_07'
    machine_type: string,      // 'press', 'compressor', 'cnc'
    measure_type: string,      // 'temperature', 'vibration', 'pressure', 'rpm'
    unit: string,              // '°C', 'mm/s', 'bar', 'rpm'
    value: real
)
```

One row per single measurement. What the value *is* is told by the `machine_type` and `measure_type` columns.

### Sample data

| ts | machine_id | machine_type | measure_type | unit | value |
|---|---|---|---|---|---|
| 2026-05-07T10:00:00 | press_01 | press | temperature | °C | 78.5 |
| 2026-05-07T10:00:00 | press_01 | press | vibration | mm/s | 2.3 |
| 2026-05-07T10:00:00 | comp_03 | compressor | temperature | °C | 65.1 |
| 2026-05-07T10:00:00 | comp_03 | compressor | oil_pressure | bar | 4.8 |

### Pros

- **Fixed, stable schema**: adding a new sensor type or machine type requires no DDL. New rows with different values in the classification columns — that's it.
- **Single ingestion pipeline**: one Eventstream → one table → one enrichment update policy. Low operational complexity.
- **Simple, powerful filters**: `where machine_type == 'press'` isolates a subset; with the right indexing and partition policy it is efficient.
- **Ideal for univariate analysis**: `series_decompose_anomalies` with `make-series ... by (machine_id, measure_type)` produces homogeneous series with no preparation.
- **Compatible with standard tooling**: the Fabric native detector (Anomaly Detector item) expects exactly this shape — one numeric column, one timestamp, one group-by column.

### Cons

- **Unsuitable for multivariate models**: a model that exploits cross-sensor correlations (e.g. Isolation Forest on `temp + vib + press`) needs to see those three features together on the same row. Pivoting from long at runtime is expensive and impractical.
- **Higher volumes**: each sampling instant produces N rows (one per signal) instead of a single row with N columns. Storage and ingestion grow proportionally.
- **Aggregate statistics on the `value` column** are physically meaningless unless filtered by `(machine_type, measure_type)`. The mean of `value` across the whole table is meaningless.
- **Uniform caching and retention**: same table = same policy. You cannot keep critical-machine data longer than the rest without advanced partitioning.

### When it makes sense

- Pure univariate: one metric at a time, independent models per `(machine_id, measure_type)`
- Many evolving sensor types
- No-code / low-code tooling (Anomaly Detector item, native KQL) as the main engine
- Small team that doesn't want to manage many tables and pipelines

---

## 3. Type B — Separate wide tables per machine type

### Structure

```kusto
.create table measures_press (
    ts: datetime,
    machine_id: string,
    temp: real,
    vib: real,
    press: real,
    rpm: real,
    cycle_count: long
)

.create table measures_compressor (
    ts: datetime,
    machine_id: string,
    temp: real,
    oil_pressure: real,
    current: real,
    duty_cycle: real
)

.create table measures_cnc (
    ts: datetime,
    machine_id: string,
    spindle_temp: real,
    axis_load_x: real,
    axis_load_y: real,
    axis_load_z: real,
    feed_rate: real
)
```

One row per sampling instant, one column per signal, one table per machine type.

### Sample data (table `measures_press`)

| ts | machine_id | temp | vib | press | rpm |
|---|---|---|---|---|---|
| 2026-05-07T10:00:00 | press_01 | 78.5 | 2.3 | 152.0 | 1200 |
| 2026-05-07T10:00:01 | press_01 | 78.6 | 2.4 | 152.3 | 1198 |
| 2026-05-07T10:00:00 | press_02 | 81.2 | 3.1 | 148.7 | 1180 |

### Pros

- **Native shape for multivariate models**: an Isolation Forest, autoencoder, or gradient boosting trained on cross-sensor correlations gets ready-to-use features, no runtime pivot.
- **Self-documenting schema**: anyone looking at the table immediately sees the press's sensors. No `measure_type` lookup is needed to understand a row.
- **Fine-grained per-type policies**: caching, retention, partitioning, sharding all independent. Presses can have 30-day caching, compressors 7, CNCs 90, each according to its own needs.
- **Natural scoring pipelines**: one anomaly-detection update policy per table, each with its own dedicated custom model. Operational tidiness.
- **Optimal scan performance**: no `where machine_type == ...` filter to apply, no "irrelevant" rows to discard.

### Cons

- **Rigid schema**: adding a new signal to an existing type requires DDL (`alter table add column`), backwards-compatibility testing, possibly a backfill.
- **Table explosion**: with 10 machine types you get 10 measure tables + 10 anomaly tables + 10 update policies + 10 scoring functions. Multiplied maintenance.
- **More complex ingestion pipelines**: routing per type at the entry point is needed, which can live in Eventstream (with multiple destinations) or in update policies starting from a raw table.
- **A new machine type = a project**: not just adding rows; you must create tables, policies, models, dashboards.
- **Incompatible with the native Anomaly Detector item** if there are many sensors, because the tool expects a single numeric column.

### When it makes sense

- Limited and stable number of machine types (typically fewer than 10)
- Multivariate ML models as the main anomaly-detection engine
- Different SLA, retention, or caching needs across types
- A structured team with well-governed DDL processes

---

## 4. Type C — Hybrid architecture (medallion)

### Structure

The idea is not to choose between A and B, but to **build both layers** so each serves the right consumer.

```
┌─────────────────────┐
│  measures_raw       │  ← Bronze: raw ingestion, long, immutable
│  (long, no enrich)  │     as it arrives from the Eventstream
└──────────┬──────────┘
           │ update policy with lookup against machines_dim
           v
┌─────────────────────┐
│  measures_enriched  │  ← Silver: long enriched with
│  (long, classified) │     machine_type, measure_type, unit
└──────────┬──────────┘
           │ update policy with pivot per type
           v
┌─────────────────────┐
│  measures_press_w   │  ← Gold: wide per type,
│  measures_compr_w   │     ready for multivariate models
│  measures_cnc_w     │
└──────────┬──────────┘
           │ update policy with scoring (Python plugin)
           v
┌─────────────────────┐
│  anomalies          │  ← unified output in long format,
│  (long, unified)    │     consumed by Activator and dashboards
└─────────────────────┘
```

### Components

**`measures_raw`** — the table fed by the Eventstream as-is, long format and without classification columns. It is not touched; it is the immutable source.

**`machines_dim`** — dimension table (master data) with the `machine_id → machine_type, measure_type, unit, nominal_min, nominal_max, plant` mapping. Maintained separately, populated from Excel or a corporate master file.

**`measures_enriched`** — enriched version of `measures_raw` with classification columns, fed by an update policy that performs a `lookup` against `machines_dim`. Stays in long format. This is the Silver layer.

**Per-type wide tables** (`measures_press_wide`, `measures_compressor_wide`, ...) — fed by update policies that pivot from `measures_enriched`, only once at ingestion. This is the Gold layer optimized for ML.

**`anomalies`** — unified table in long format that receives the outputs of all models (one per machine type), with `machine_type`, `model_version`, `score`, `is_anomaly`, ... columns. Single source of truth for Activator and dashboards.

### Example: enrichment update policy

```kusto
.create-or-alter function EnrichMeasures() {
    measures_raw
    | lookup kind=leftouter machines_dim on machine_id
    | project ts, machine_id, machine_type, measure_type, unit, value
}

.alter table measures_enriched policy update 
@'[{"IsEnabled": true, "Source": "measures_raw", "Query": "EnrichMeasures()", "IsTransactional": false}]'
```

### Example: pivot update policy for presses

```kusto
.create-or-alter function PivotPress() {
    measures_enriched
    | where machine_type == 'press'
    | summarize 
        temp  = anyif(value, measure_type == 'temperature'),
        vib   = anyif(value, measure_type == 'vibration'),
        press = anyif(value, measure_type == 'pressure'),
        rpm   = anyif(value, measure_type == 'rpm')
        by ts = bin(ts, 1s), machine_id
}

.alter table measures_press_wide policy update 
@'[{"IsEnabled": true, "Source": "measures_enriched", "Query": "PivotPress()", "IsTransactional": false}]'
```

### Pros

- **Each consumer gets the right shape for its job**: the univariate detector and native KQL queries operate on `measures_enriched` (long), multivariate models operate on the per-type wide tables, the alert dashboard operates on `anomalies` (unified long).
- **Decoupling from the source**: if tomorrow the ingestion team fixes the missing columns, only the lookup layer goes away; the rest of the pipeline doesn't change.
- **Single source of truth for alerts**: a single `anomalies` table to watch with Activator, regardless of how many models/types are behind it.
- **Gradual evolution**: start with `measures_enriched` + the native Anomaly Detector item, and add wide tables and custom models only when needed. You only pay the complexity when you need it.
- **Cross-cutting reusability**: enrichment is not specific to anomaly detection. It is reused by Power BI for dashboards, Lakehouse for reporting, and any future consumer.

### Cons

- **More tables to govern**: three layers instead of one. You have to document what is authoritative and what is derived.
- **Additional latency between layers**: each update policy adds a few seconds of delay. For typical near real-time use cases (tens of seconds) it is negligible, but it should be considered.
- **Multiplied storage**: same data in multiple shapes. In Eventhouse the cost is contained thanks to compression, but it is not zero. Compensate with aggressive retention policies on the derived layers (e.g. 7 days hot, keeping history only in raw).
- **Discipline required on `machines_dim`**: if the master data is not up-to-date, enrichment leaves rows with `machine_type` null and the entire downstream chain drops them. It must be monitored.

### Two operational gotchas with Python-based update policies

These bite anyone who builds the scoring layer for the first time. They are unrelated to data modelling per se, but they break the whole pipeline if missed:

1. **Source table must use queued (not streaming) ingestion.** The `python()` plugin is not allowed in update policies whose source table has streaming ingestion enabled. In **Microsoft Fabric Eventhouse, streaming is the default** for every newly created table, so a fresh Eventstream-backed table will silently fail to score. Disable it once on the source:

   ```kusto
   .alter table measures_raw policy streamingingestion '{"IsEnabled": false}'
   ```

   Latency moves from <1 s to 5-30 s — fine for any anomaly-detection scenario.

2. **Per-extent rewrite of direct table references inside the function.** The engine rewrites `measures_raw` (or any direct ref to the source table) inside the update-policy query as `__table("measures_raw") | where extent_id() in (guid(<just-ingested-extent>))`. For window-based scoring (e.g. an autoencoder over 64 samples per `(machine, sensor)`) a single extent must contain enough contiguous data to build complete windows, and windows that *straddle* the boundary between two batches would otherwise be silently lost.

   The clean production pattern combines two things:

   - Pin the source table's batch cadence with an explicit `ingestionbatching` policy so each batch contains hundreds of samples per (machine, sensor):

     ```kusto
     .alter table measures_raw policy ingestionbatching
     '{ "MaximumBatchingTimeSpan": "00:01:00", "MaximumNumberOfItems": 25000, "MaximumRawDataSizeMB": 1024 }'
     ```

   - In the scoring function, read the new extent directly from `measures_raw` (extent-filtered) and pull only **`window_size − 1` preceding rows** of left context indirectly via `database()` to complete any boundary-straddling window. Then keep only windows whose `window_end` is in the new batch:

     ```kusto
     let new_data = measures_raw  // extent-filtered
         | where machine_id == m and sensor_id == s | project ts, value;
     let new_ts_min = toscalar(new_data | summarize min(ts));
     let context = database(current_database()).measures_raw
         | where machine_id == m and sensor_id == s and ts < new_ts_min
         | top (window_size - 1) by ts desc | project ts, value;
     union context, new_data
     | order by ts asc
     | extend rn = row_number() - 1, win_id = rn / window_size
     | summarize window_end = max(ts), values = make_list(value) by win_id
     | where array_length(values) == window_size
     | where window_end >= new_ts_min   // each window scored exactly once
     ```

   Cost stays proportional to the new data, every window is scored exactly once across the lifetime of the pipeline, and there are no duplicates to dedupe downstream. A naïve `where ts > now() - 10m` scan inside an update policy works for ad-hoc queries from a notebook but produces duplicates and re-reads the same rows on every batch — avoid it in production.

### When it makes sense

- Source schema sub-optimal and not immediately changeable
- Mix of consumers with different needs (univariate, multivariate, dashboard, reporting)
- Volumes that justify materialization rather than runtime pivots
- A team that wants to separate responsibilities across layers (data engineering on Silver, data science on Gold)

---

## 5. Side-by-side comparison

| Criterion | A — Single long | B — Wide per type | C — Hybrid |
|---|---|---|---|
| Initial complexity | Low | Medium | High |
| Number of tables | 1 | N types | 1 + 1 + N + 1 |
| Suited to univariate (native KQL) | Excellent | Awkward | Excellent (on Silver) |
| Suited to multivariate (custom ML) | Awkward | Excellent | Excellent (on Gold) |
| Compatible with Anomaly Detector item | Yes | No (multiple sensors) | Yes (on Silver) |
| Schema evolution (new sensor) | Zero impact | DDL on the table | DDL only on the affected Gold |
| Evolution (new machine type) | Zero impact | New table + policy | New Gold + policy, Silver unchanged |
| Differentiated retention/caching | Hard | Easy | Easy (on Golds) |
| Single source of truth for alerts | Yes | Must be built | Yes (`anomalies`) |
| Decoupling from the source | No | No | Yes |
| Storage overhead | Minimal | Minimal | Moderate |

---

## 6. Recommendation

For the use case described — industrial measurements from heterogeneous machines, current source in long format without type columns, goal of multivariate near real-time anomaly detection — the recommended choice is **Type C — hybrid architecture**.

Main reasons:

1. **It requires no source changes**. The current schema, although sub-optimal, is absorbed without trauma. The modeling debt remains documented but does not block the project.

2. **It serves both anomaly-detection patterns**. Native KQL functions and the Anomaly Detector item work on the long-format Silver; custom multivariate models (Isolation Forest, autoencoder, etc.) work on per-type wide Golds. No forced compromise.

3. **It scales with machine heterogeneity**. Adding a new machine type means adding a new Gold table and a new pivot update policy, without touching Silver or the source. Adding a new machine of an existing type is zero-DDL: just update `machines_dim`.

4. **Single source of truth for alerting**. The `anomalies` table collects outputs from all models, regardless of machine type. Activator has a single source to watch, dashboards a single table to query, drift monitoring a single place to analyze.

5. **Incremental evolution**. You don't have to build everything at once. You can start with just Silver + the Anomaly Detector item to validate the approach, and introduce Golds + custom models only for types that need them. Complexity is paid in proportion to the value obtained.

6. **Alignment with the medallion pattern**. The raw → enriched → per-type → anomalies structure is exactly the Bronze/Silver/Gold pattern that Fabric promotes as a standard. Documentation, tooling, and best practices are aligned.

### When to deviate from the recommendation

- **Toward A** if anomaly detection will be exclusively univariate and you want to minimize setup effort. It's the right choice if the goal is "alert on a single sensor outside an adaptive threshold" and cross-sensor correlation is not interesting.

- **Toward B** if the machine types are very few (two or three) and no growth is expected, and you want to maximize operational tidiness. Suited to contexts with a single industrial product and very stable processes.

### How this repo implements Type C

The demo in this repository is a minimal but faithful Type C deployment:

- **Bronze / Silver** — a single long table `raw_telemetry` (one row per measurement, columns `ts`, `machine_id`, `sensor_id`, `value`, `unit`).
- **Gold** — instead of building per-type wide tables via update-policy pivots (as described in §4), the repo uses a **materialized view** `raw_telemetry_wide_mv` that pivots `raw_telemetry` into one row per `(machine_id, ts_bin = 1s)` with one column per sensor. The MV is auto-maintained by Eventhouse on every ingest, so the Gold layer needs no orchestration code and costs ~1× storage.
- **Anomaly fan-in** — a single `anomalies` table fed by **two** update policies on `raw_telemetry`: `fn_score_demo()` (univariate per-sensor LSTM AE) and `fn_score_multivariate_demo()` (multivariate per-machine LSTM AE over the wide MV). Rows are distinguished by `model_name` (`univariate_ae__*` vs `multivariate_ae__*`).
- **Lookup table deferred** — `machines_dim` is not yet implemented; the multivariate model is currently per-machine with a hard-coded `machine_id`. A small routing table is the next natural extension.

The materialized view is a lighter-weight Gold materialization than the update-policy pivot pattern of §4: it has no scheduling, no double-write to keep in sync, and adapts to backfills automatically. The trade-off is that the column list is fixed at MV definition time — adding a sensor requires a `.create-or-alter materialized-view` (and a model retrain).

For the full deployed shape see [`./architecture.md`](./architecture.md) §3 and [`./anomaly_detection_fabric_kql.md`](./anomaly_detection_fabric_kql.md) §4.6.

---

## 7. Implementation outline of the recommended solution

Recommended order of implementation:

1. **`machines_dim` master data** — dimension table populated from a master file. Even manual at first is fine.
2. **`measures_enriched`** + lookup update policy — Silver layer. From this point on, all new consumers point here.
3. **Native Anomaly Detector item on `measures_enriched`** — first univariate anomaly-detection engine, in production within days. Lets you validate the approach while the rest is being built.
4. **Unified `anomalies` table** — schema shared between the native detector and future custom models. Activator attached here.
5. **One Gold wide table for the most critical machine type**, chosen by business value or anomaly volume. Pivot update policy.
6. **Custom multivariate model on the first Gold** — training notebook, `models` table, scoring function, update policy that writes into `anomalies`.
7. **Replicate (5) and (6) on the other types**, one at a time.
8. **Drift monitoring and scheduled retraining** — only after the system is in production and has accumulated a few weeks of observation.

In parallel, regardless of technical progress, it is worth opening a discussion with the team that owns ingestion to bring the `machine_type` and `measure_type` columns to the source. It is a debt worth settling in the medium term, even though the hybrid architecture makes it non-blocking in the short term.
