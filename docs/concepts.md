# Anomaly detection on Microsoft Fabric — concepts

A plain-English tour of what is going on in this repo. No commands, no KQL,
no Python — just the ideas you need to understand why the architecture looks
the way it does.

For the executable details, see:

- [`anomaly_detection_fabric_kql.md`](anomaly_detection_fabric_kql.md) —
  the cookbook (full code, every option).
- [`data_modeling_industrial_measures.md`](data_modeling_industrial_measures.md) —
  how to shape the tables when measurements come in heterogeneously.
- [`architecture.md`](architecture.md) — the deployed pieces of this demo.

---

## 1. The problem in one sentence

Industrial machines emit a constant stream of measurements (temperature,
vibration, pressure, current, …) and we want a system that, **as soon as the
measurements look "wrong" compared to normal behaviour**, raises an alert —
in seconds, not hours.

That breaks down into three sub-problems:

1. **Get the data in fast and reliably.** A factory floor doesn't pause.
2. **Decide what "wrong" means.** Either a hand-written rule, a statistical
   test on the signal, or a machine-learning model trained on normal history.
3. **React.** Email, Teams, ticket, automation — something a human or a
   downstream system can act on.

The whole demo is built around those three layers.

---

## 2. Why Microsoft Fabric, and why Eventhouse specifically

Fabric is Microsoft's "all-in-one" data and analytics platform. Inside Fabric
the workload that fits real-time telemetry is **Real-Time Intelligence**, and
its main storage engine is the **Eventhouse**.

An Eventhouse is essentially a hosted Kusto cluster (the same engine behind
Azure Data Explorer / Application Insights / Sentinel). What makes it the
right tool here:

- **Built for append-only time-series**: billions of rows per day are normal.
- **Sub-second queries on recent data**, because hot data lives in memory.
- **Native time-series functions** (decomposition, anomaly detection,
  forecasting) — no Python required for the simple cases.
- **Built-in Python sandbox** for the harder cases (custom ML models, ONNX,
  PyTorch, scikit-learn).
- **Eventstream** in front of it: a managed pipe that takes data from IoT
  Hub, Event Hubs, Kafka, custom apps and lands it into Eventhouse tables
  without code.

In short: a single product covers ingestion, storage, querying, ML scoring
and alerting. That is the value proposition.

---

## 3. The pipeline at a glance

```
┌──────────┐     ┌────────────┐     ┌────────────────┐     ┌───────────┐     ┌──────────┐
│ Machines │ ──> │ Eventstream│ ──> │   Eventhouse   │ ──> │  Reflex   │ ──> │  Alerts  │
│ (or sim) │     │  (managed  │     │  raw_telemetry │     │ (a.k.a.   │     │  Teams,  │
│          │     │   pipe)    │     │       │        │     │ Activator)│     │  email   │
└──────────┘     └────────────┘     │       v        │     └───────────┘     └──────────┘
                                    │   anomalies    │
                                    │ (scored rows)  │
                                    └────────────────┘
```

Two tables, one mechanism that promotes rows from the first to the second
when they look anomalous, and one watcher that fires alerts on the second.
That's the whole picture.

---

## 4. The two tables you really need to understand

### `raw_telemetry`

The "everything that happened" table. One row per measurement:

| machine_id | sensor_id        | ts                   | value |
|------------|------------------|----------------------|-------|
| M-001      | temperature_motor| 2026-05-14 11:24:00  | 62.31 |
| M-001      | vibration_radial | 2026-05-14 11:24:00  | 0.42  |
| …          | …                | …                    | …     |

It grows continuously. It is **the source of truth**. Every analysis is
ultimately derived from it.

### `anomalies`

A much smaller, derived table. One row per **window** of measurements that
the model considered abnormal:

| detected_at | machine_id | sensor_id        | window_start | window_end | score   | is_anomaly |
|-------------|------------|------------------|--------------|------------|---------|------------|
| 11:24:17    | M-001      | temperature_motor| 11:23:14     | 11:24:17   | 3866.59 | true       |

This is what dashboards and alerting watch. It is small, opinionated, and
business-meaningful.

---

## 5. The key idea: an "update policy" turns the first table into the second

An **update policy** is a built-in Eventhouse mechanism that says:

> "Every time new data lands in table A, run this query and write its
> output into table B."

In our case:

- A = `raw_telemetry` (new rows arriving from the factory)
- B = `anomalies`
- The query in the middle is a **scoring function** that loads the trained
  model, builds windows of recent measurements, runs each window through
  the model, and emits only the windows whose score exceeds a threshold.

Result: rows flow into `raw_telemetry` from Eventstream and, a few seconds
later, anomalous windows show up in `anomalies` — automatically, with no
external job, no scheduler, no orchestrator.

This is the heart of the architecture. Everything else exists to make this
mechanism work properly.

---

## 6. The two ingestion modes — and why this matters here

When you create a table in Eventhouse you implicitly pick one of two
ingestion **modes**. They do the same thing (rows in → rows on disk) but
with very different timing and very different rules.

### Streaming ingestion (default in Fabric Eventhouse)

- Each individual row is committed to the table **as soon as it arrives**.
- Latency: well under 1 second.
- Internally the engine buffers rows in a small, fast-changing structure
  optimised for "write now, organise later".
- **Side effect**: the engine has very limited time and visibility to do
  expensive things at write time. It cannot, for example, spin up a Python
  sandbox for every row.

### Queued (batch) ingestion

- Rows are **accumulated** for a few seconds (typically 5–30) and then
  written in one shot as a proper, fully-organised "extent" (Kusto's word
  for an immutable batch of data).
- Latency: 5–30 seconds.
- Because the engine writes a coherent batch, it can run heavier work as
  part of the write — including custom code in the Python sandbox.

### Why this matters for us

The Python plugin **only works inside an update policy whose source table
uses queued ingestion**. The reason is straightforward: streaming ingestion
optimises for "land each row in milliseconds" and is fundamentally
incompatible with stopping to call out to a Python sandbox.

In Fabric Eventhouse, streaming is the default for every new table. So when
we attach an Eventstream to `raw_telemetry`, by default we end up with
sub-second latency but **no scoring**. The fix is one administrative
command that flips `raw_telemetry` to queued mode. We trade a few seconds
of additional ingestion latency for the ability to run ONNX models on
every batch — generally an acceptable trade-off for industrial anomaly
detection, where reaction times are typically measured in seconds to
minutes rather than milliseconds.

> **The mental model**: streaming = "fire hose, no inspection". Queued =
> "small, regular truckloads that you have time to inspect". Custom ML
> scoring lives in the inspection step.

---

## 7. "Native" anomaly detection vs "custom" anomaly detection

Eventhouse offers two completely different ways to spot anomalies. They
solve different problems and have very different complexity profiles.

### Native KQL functions (the easy path)

Functions like `series_decompose_anomalies` work directly on a time-series
expression. Under the hood they:

1. Decompose the signal into seasonality + trend + residual.
2. Apply a statistical test (Tukey-style) to the residual.
3. Flag points that are too far from the expected baseline.

**Pros**: zero ML expertise, no model lifecycle, runs in pure KQL,
vectorised across thousands of series in parallel, no Python sandbox.

**Cons**: it sees the signal as a 1-D number sequence. It cannot reason
about the *shape* of recent activity, cannot combine multiple sensors,
cannot learn an arbitrary "this is what normal looks like" boundary.

This is the right starting point for 80% of the use cases.

### Custom models in the Python sandbox (the powerful path)

When the native functions are not enough — typically because the anomaly is
about *shape* (vibration patterns, multi-sensor combinations, autoencoder
reconstruction error) — you train a model offline, store it inside
Eventhouse as a base64 blob, and have the scoring function load it and run
it on each batch through the Python plugin.

This repo demonstrates the custom path with a tiny **autoencoder**, exported
to **ONNX** for portability and small footprint. The training notebook
writes the model into a `models` table; the scoring function reads the
latest row from that table at runtime.

**Pros**: arbitrary models, shape-aware, multi-sensor, retrainable.

**Cons**: model lifecycle to manage, sandbox limits to respect (no network,
limited memory, must keep the pickled/ONNX payload small).

You can absolutely **mix the two**: native decomposition for cheap, broad
coverage; custom models for the few high-value machines or KPIs that
deserve a tailored model.

---

## 8. Why we use ONNX (and why the autoencoder is tiny)

The Python sandbox inside Eventhouse is a constrained environment:

- A few seconds of CPU per batch.
- Limited memory.
- No internet.
- Whatever runs has to fit inside the model row stored in the `models`
  table (we ship the model as base64 inside the row itself).

ONNX is a model-serialisation format that:

- Is much smaller than equivalent TensorFlow/PyTorch checkpoints.
- Is loaded by `onnxruntime`, which is pre-installed in the DL Python image
  and is fast and memory-frugal.
- Is **framework-agnostic**: train in PyTorch or TensorFlow, export to
  ONNX, score in any runtime that speaks ONNX.

Our autoencoder takes a window of 64 measurements of one sensor on one
machine, tries to "reconstruct" it, and outputs a reconstruction error.
The intuition: if the model was trained on normal behaviour only, then the
more anomalous the input, the worse the reconstruction → the higher the
error → that's our anomaly score.

We then pick a **threshold** on that score (e.g. the 95th percentile of
recent normal scores). Above the threshold → row goes into `anomalies`.

---

## 9. Window-based models need a small slice of history across batch boundaries

The autoencoder needs **64 contiguous samples for the same (machine,
sensor)** to score one window. With a properly sized batch (~1 minute,
hundreds of samples per sensor — see the `ingestionbatching` policy
below) most windows are built entirely from the new batch. The only
windows that need extra care are the ones that **straddle the boundary**
between this batch and the previous one: their first ~63 samples landed
earlier and only the last sample arrives now. This section explains
how the engine sees the data, why those boundary windows matter, and
how we stitch them back without scanning history on every batch.

When the engine fires the update policy, it doesn't run the function
"as written". It transparently injects a filter so that, inside the
function, every direct mention of `raw_telemetry` only resolves to the
rows of the **single new batch** — not to the whole table. (Internally
this is done by wrapping the table reference with a `where extent_id() in
(...)` clause that names the batch's storage unit.) The intent is
performance: the function should only re-process what actually arrived,
not rescan the entire history on every trigger.

That behaviour is great for point-wise scoring (one row in, one score
out): it keeps the work proportional to the new data.

But how big is "the new batch"? That is governed by the table's
**ingestion batching policy**. If batches are too small (a default
behaviour under high event rates is to close on the row cap after only a
few seconds), most batches won't even contain 64 samples for a given
(machine, sensor). We pin this down explicitly in `kql/01_tables.kql`:

```kusto
.alter table raw_telemetry policy ingestionbatching
{ "MaximumBatchingTimeSpan": "00:01:00", "MaximumNumberOfItems": 25000, ... }
```

That gives ~1-minute batches with hundreds of samples per sensor —
enough to build several full windows per batch.

But one batch boundary will always cut a window in half: the first ~63
samples of a "boundary window" landed in the previous batch, the last
sample lands in the new one. If we ignored those, we would silently lose
one window per batch per (machine, sensor). The scoring function therefore
does one tiny extra read: it pulls the **last `window_size - 1` samples**
that arrived **before** this batch, via an indirect reference to the
table:

```kusto
let context = database(current_database()).raw_telemetry
    | where machine_id == machine and sensor_id == sensor and ts < new_ts_min
    | top (window_size - 1) by ts desc;
```

The indirect form (`database(current_database()).raw_telemetry`) is **not**
rewritten by the engine — that's exactly what we need to escape the
per-batch filter for this specific small read. We then `union` the
context with the new batch, build windows, and emit only those whose
`window_end` falls inside the new batch — so each window is scored
**exactly once across the lifetime of the pipeline**:

```kusto
| where window_end >= new_ts_min   // skip windows fully in the past
```

The cost is bounded: at most `window_size - 1` extra rows per (machine,
sensor) per batch, regardless of how long the pipeline has been running.
No duplicates, no data loss, latency ≈ batching window.

That's the production pattern, codified in `score_univariate_onnx_batch`
in `kql/03_scoring_functions.kql`. A separate `score_univariate_onnx_lookback`
exists for ad-hoc use from notebooks (it scans a lookback window from the
full table — fine for exploration, but **not** safe inside an update policy
because it would re-emit the same windows on every batch).

The right mental model: **update policies are designed for proportional
work on the new batch**. A window-based model breaks that assumption only
at the batch boundary, and the cure is a tiny bounded read of historical
context — not a periodic full rescan.

---

## 10. Reflex — turning rows in `anomalies` into actual alerts

The `anomalies` table is just data. Nobody is watching it 24/7. We need
something that *reacts* when a new row appears.

That something is **Reflex** (also called Activator in the UI). It's a
Fabric item that:

- Continuously watches a stream or a KQL query.
- Evaluates a rule ("is_anomaly == true", or "score > 4000", or
  "more than 5 anomalies in the last 10 minutes for the same machine").
- Triggers an action: send a Teams message, send an email, call a
  webhook, run a Power Automate flow, kick off a Fabric pipeline.

Reflex closes the loop: from raw measurement at the edge to a Teams ping
in the maintenance team's channel, with no glue code in between.

For this demo Reflex is configured manually in the portal (it's a
point-and-click experience and it doesn't fit naturally in a CLI script).

---

## 11. Where the model comes from — the offline training notebook

The model is trained outside the real-time pipeline, on **historical**
data. The flow is:

1. A Fabric Spark notebook reads months of `raw_telemetry` (or a Lakehouse
   copy of it) for the period considered "normal".
2. It builds 64-sample windows per (machine, sensor) and fits an
   autoencoder so that reconstruction error is low on normal windows.
3. It exports the trained network to ONNX.
4. It writes the ONNX bytes (base64-encoded) plus metadata into the
   `models` table inside the same Eventhouse.

The runtime scoring function picks the **latest** row of the `models`
table at every invocation — so retraining means: re-run the notebook,
write a new row. The next batch automatically uses the new model. No
deployment, no restart, no service.

---

## 12. The multivariate variant — same idea, wider input

Everything above is described for one sensor at a time (univariate). The
repo also ships a **multivariate** model that watches **all 8 sensors of a
machine jointly** and emits one anomaly score per window. The mental model
is identical — train an autoencoder on normal windows, score the
reconstruction error, threshold it — but two pieces have to be added.

### 12.1 A wide "shape" of the data, maintained for free

The scoring function needs to see all sensors of one machine on the same
row at the same instant. Computing that pivot inside the update policy on
every batch would be wasteful, so we let Eventhouse maintain it for us
with a **materialized view** (`raw_telemetry_wide_mv`):

| ts_bin              | machine_id | temperature_motor | vibration_radial | current | … |
|---------------------|------------|-------------------|------------------|---------|---|
| 11:24:00            | M-001      | 62.31             | 0.42             | 11.7    | … |
| 11:24:01            | M-001      | 62.40             | 0.41             | 11.6    | … |

One row per `(machine_id, ts_bin = 1s)`, one column per sensor. The MV is
updated on every ingest into `raw_telemetry`, costs ~1× storage thanks to
bin-row reconciliation, and gives the multivariate scoring function its
input in the right shape with no run-time work.

### 12.2 A second update policy on the same `anomalies` table

Multiple update policies can target the same destination table. The repo
attaches a second one alongside the univariate scorer; rows are
distinguished downstream by `model_name` (`univariate_ae__*` vs
`multivariate_ae__*`). One alert table, several model families, one
Reflex.

### 12.3 Two practical wrinkles worth knowing

- **Per-feature normalization is baked into the ONNX graph.** The
  multivariate notebook fits per-feature `mean` and `std` and stores them
  as constant buffers in a tiny `NormalizedScoreWrapper` layer of the ONNX
  export. This means the KQL function passes raw sensor values from the
  MV — no scaler step in the function, no risk of train/score drift if
  somebody forgets to keep the two in sync.
- **An anchor-sensor dedup filter avoids firing the policy 8× per batch.**
  A long table with N sensors per machine generates N rows per `ts_bin`
  per machine. Without care, the multivariate update policy would fire ~N×
  on every ingest. The function therefore checks that the new batch
  contains at least one row of an *anchor sensor* (here
  `temperature_motor`) before producing any output. With 8 sensors this
  divides the scoring rate by ~8 without losing any window. The trade-off:
  if the anchor sensor stops reporting, multivariate scoring stops too —
  pick a sensor that's guaranteed to be present, or move to a small
  routing table for production.

### 12.4 Threshold lives with the model, not with the function

For the univariate model the threshold is a literal in `fn_score_demo()`
(`threshold = 3870.0`); calibration means redeploying the function. For
the multivariate model the threshold is computed at training time as
`mean(loss) + K · std(loss)` (with `K = 4` on normalized features) and
stored in `metadata.threshold` of the model row. The KQL scoring function
reads it from there — retraining is enough to re-calibrate, no KQL
redeploy needed.

---

## 13. Summary — the design decisions in one place

| Decision | Why |
|---|---|
| Use Eventhouse, not a generic SQL DB | Built for append-only time-series, sub-second queries, integrated Python sandbox |
| Use Eventstream as the front door | Managed connector to IoT/Event Hubs/Kafka, no code |
| Two-table model (`raw_telemetry` + `anomalies`) | Separates "everything" from "interesting"; alerting only watches the small table |
| Update policy as the bridge | No external scheduler / orchestrator; the engine itself promotes anomalous rows |
| Disable streaming on `raw_telemetry` | Required so the Python plugin is allowed in the update policy; ~10s latency is fine for industrial AD |
| ONNX autoencoder, stored inline in `models` | Small, fast, framework-agnostic, sandbox-friendly |
| Batch-only scoring + `(window_size − 1)` left-context read | Each window scored exactly once, cost proportional to new data, no duplicates |
| Explicit `ingestionbatching` policy on `raw_telemetry` | Pin batch cadence (~1 min) so each batch contains enough samples per (machine, sensor) for full windows |
| Reflex on `anomalies` | Native, code-free way to turn rows into Teams/email/automation |
| Native KQL anomaly functions kept as a fallback | Cover the easy 80% with zero ML work |
| **Multivariate**: wide materialized view fed by `raw_telemetry` | One pivot maintained for free, no run-time pivot in the policy |
| **Multivariate**: per-feature normalization baked into the ONNX graph | KQL stays stateless; no train/score skew |
| **Multivariate**: threshold stored in the model's `metadata` | Retraining is enough to recalibrate; no KQL redeploy |
| **Multivariate**: anchor-sensor dedup in the entry-point function | Update policy fires ~1× per batch instead of N× (one per sensor row) |

---

## 14. What you would change in production

This repo is a demo. In a real deployment you would typically:

- Have **one model per (machine_type, sensor)** routed via a small lookup
  table, not a hard-coded `(M-001, temperature_motor)`.
- Add a **Silver enrichment layer** that joins `raw_telemetry` with a
  `machines_dim` master table to attach machine type, line, plant, etc.
  (See `data_modeling_industrial_measures.md`.)
- Calibrate the **threshold per machine** (and re-calibrate on a schedule)
  rather than using a single global value. The multivariate model already
  stores its threshold in metadata — extend the same convention to the
  univariate scorer.
- Track each model version in **MLflow** for proper lineage.
- Deduplicate in the scoring function rather than at query time.
- Add a **retention policy** (e.g. 90 days hot, longer cold) on
  `raw_telemetry`, and a separate one on `anomalies`.
- Hook Reflex into a **ticketing system** (ServiceNow, Jira) instead of
  just Teams, so anomalies become trackable work items.
- Replace the single anchor-sensor dedup with a small helper function
  that picks the densest sensor per `(machine, batch)` so multivariate
  scoring is robust to single-sensor outages.

None of these change the architecture; they just harden it.
