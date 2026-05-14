# Architecture — Factory Anomaly Detection on Microsoft Fabric

> For the *why* behind these choices (in plain English, no code), read
> [`concepts.md`](concepts.md) first. This document focuses on the *what*:
> the items deployed by `scripts/deploy.ps1` and how they are wired up.

## 1. Goal

Detect anomalies in real time across **multiple factory machines**, each
producing **multiple sensor measures** (temperature, vibration, pressure,
current, …). The detection model:

- looks at a **time window** of values rather than a single row,
- can be **univariate** (one window per sensor) or **multivariate** (window
  spanning several sensors of the same machine),
- runs **inside Fabric** without an external Spark/AKS cluster,
- is trained offline and **exported to ONNX** for in-cluster inference.

## 2. High-level flow

```
[machines × sensors] ──AMQP/Kafka──> Eventstream  (es_machines)
                                          │
                ┌─────────────────────────┼─────────────────────────┐
                v                                                   v
  Eventhouse / KQL Database (eh_telemetry / kql_telemetry)    Lakehouse (lh_telemetry)
   ├─ raw_telemetry          (hot store, 30d retention)        ├─ Tables/bronze_telemetry
   │     │                                                     ├─ Tables/silver_telemetry
   │     └─ materialized view raw_telemetry_wide_mv            ├─ Tables/gold_windows_uni
   │           (1 row per (machine_id, ts_bin=1s);             └─ Tables/gold_windows_multi
   │            one column per sensor — Gold for multivariate)        ^
   ├─ models                 (versioned ONNX bytes)                   │
   ├─ scoring functions      (build window → python(onnx))            │
   │     ├─ univariate    : score_univariate_onnx_batch / _lookback   │
   │     └─ multivariate  : score_multivariate_onnx_batch_from_mv     │
   ├─ update policy on `anomalies`                                    │
   │     ├─ fn_score_demo()              (univariate, fires per batch)│
   │     └─ fn_score_multivariate_demo() (multivariate, anchor-dedup) │
   └─ anomalies              (detections; 365d retention)             │
                ^                                                     │
                │                                                     │
   Reflex (act_anomaly_alerts)               Notebook 04_train_univariate_ae   ──> models (univariate_ae__<sensor>)
        Teams / email / pipeline trigger     Notebook 05_train_multivariate_ae ──> models (multivariate_ae__<machine>)
                                                                  ^
                                                                  │
                                                Data Pipeline (pl_retrain) — schedule

Semantic Model (sm_anomaly) <── KQL DB + Lakehouse gold ──> Report (rpt_anomaly)
```

The materialized view `raw_telemetry_wide_mv` is the key piece that enables
multivariate scoring without a runtime pivot: it is maintained automatically
by Eventhouse on every ingest into `raw_telemetry`, costs ~1× storage thanks
to bin-row reconciliation, and serves as the Gold-tier wide table the
multivariate scoring function reads from.


## 3. Items provisioned by `scripts/deploy.ps1`

| Item             | Name (default)         | Type           | Purpose                                                      |
|------------------|------------------------|----------------|--------------------------------------------------------------|
| Workspace        | `anomaly-detection-dev`| Workspace      | Container; bound to the configured capacity                  |
| Eventstream      | `es_machines`          | Eventstream    | Single ingestion endpoint for all machines (custom-app source); fan-out to KQL + Lakehouse |
| Eventhouse       | `eh_telemetry`         | Eventhouse     | Hosts the KQL database                                       |
| KQL Database     | `kql_telemetry`        | KQLDatabase    | Hot store + ONNX scoring + anomaly table                     |
| Lakehouse        | `lh_telemetry`         | Lakehouse      | Cold store + medallion tables for training                   |
| Environment      | `env_anomaly`          | Environment    | Pinned PySpark libs (torch, onnx, onnxruntime, scikit-learn, skl2onnx, azure-kusto-data). See §4.6 about the runtime install fallback. |
| Notebook         | `nb_prepare_features`  | Notebook       | (legacy scaffold) bronze → silver → gold windows. Superseded by the wide MV — see §4.3. |
| Notebook         | `nb_train_export_onnx` | Notebook       | (legacy scaffold) generic single-model trainer. Superseded by `nb_04_train_univariate_ae` and `nb_05_train_multivariate_ae`. |
| Notebook         | `nb_register_kql_scorer`| Notebook      | Reapplies `kql/*.kql` to the database                        |
| Notebook         | `nb_04_train_univariate_ae`   | Notebook | Per-sensor LSTM autoencoder. Trains on `raw_telemetry`, exports ONNX, uploads as `univariate_ae__<sensor_id>`. Published from [`notebooks/04_train_univariate_ae.ipynb`](../notebooks/04_train_univariate_ae.ipynb). |
| Notebook         | `nb_05_train_multivariate_ae` | Notebook | Per-machine LSTM autoencoder over 8 sensors via the wide MV. ONNX wrapper has per-feature normalization baked in. Threshold (`mean(loss) + K·std(loss)`) is stored in `metadata.threshold` so KQL doesn't hard-code it. Published from [`notebooks/05_train_multivariate_ae.ipynb`](../notebooks/05_train_multivariate_ae.ipynb). |
| Data Pipeline    | `pl_retrain`           | DataPipeline   | Schedules the active training notebooks (04, 05) and `nb_register_kql_scorer` |
| Reflex           | `act_anomaly_alerts`   | Reflex         | Reacts on rows landing in the `anomalies` table              |
| Semantic Model   | `sm_anomaly`           | SemanticModel  | Direct Lake model over gold + anomalies                      |
| Report           | `rpt_anomaly`          | Report         | Operational dashboard                                        |

All container items are created **blank**. The legacy `nb_prepare_features`,
`nb_train_export_onnx`, `nb_register_kql_scorer` ship with starter scaffolds
in `items/<name>.Notebook/`; the active training notebooks are authored
locally under `notebooks/` and published with `tools/upload_notebook.py`
(see §4.6). Schema, scoring logic, Eventstream wiring and Reflex rules are
configured separately — see §4. Item names use underscores because some
Fabric item types (Eventstream, Reflex, …) reject hyphens; the defaults are
defined in `.env.example` and can be overridden in `.env`.

## 4. Post-deploy configuration

The CLI deploy gives you the empty containers. The shape of the system is
applied next:

### 4.1 Eventstream wiring (portal, one-off)

1. Open `es_machines` → **Add source** → **Custom App**. Note the AMQP /
   Kafka connection string; share it with the device fleet.
2. **Add destination** → **Eventhouse** → select `kql_telemetry`, target
   table `raw_telemetry`, mapping `raw_telemetry_json` (created in §4.2).
3. **Add destination** → **Lakehouse** → `lh_telemetry`, table
   `bronze_telemetry`, Delta format.

Telemetry payload contract (JSON):

```json
{
  "machineId": "M-001",
  "sensorId":  "temperature_motor",
  "ts":        "2026-05-07T10:15:23.451Z",
  "value":     72.3,
  "quality":   192
}
```

### 4.2 KQL schema

Run the scripts in `kql/` against `kql_telemetry`, in order:

| File                          | Creates                                           |
|-------------------------------|---------------------------------------------------|
| `01_tables.kql`               | `raw_telemetry`, `anomalies`, JSON mapping, batching policy, streaming-OFF on `raw_telemetry` |
| `02_models.kql`               | `models` table + `latest_model()` helper          |
| `03_scoring_functions.kql`    | Univariate: `build_univariate_windows_batch/_lookback`, `score_univariate_onnx_batch/_lookback`. Multivariate (raw): `build_multivariate_windows_batch`. |
| `04_update_policy.kql`        | `fn_score_demo` + update policy entry on `anomalies` (univariate) |
| `05_multivariate_mv.kql`      | Materialized view `raw_telemetry_wide_mv`, `build_multivariate_windows_batch_from_mv`, `score_multivariate_onnx_batch_from_mv`, `fn_score_multivariate_demo` + the **second** update policy on `anomalies` (multivariate, anchor-sensor dedup) |

Apply them in numeric order. Three options:

- **Portal**: open the KQL queryset attached to the database, paste each file, run.
- **Script** (recommended for repeatable deploys): run
  `python tools/02_setup_kql_tables.py kql/01_tables.kql kql/02_models.kql kql/03_scoring_functions.kql kql/04_update_policy.kql kql/05_multivariate_mv.kql`
  (auth via the same `.env` used for `scripts/deploy.ps1`).
- **Notebook**: run `nb_register_kql_scorer` (uploads `kql/` to the lakehouse Files area first).

> **Prerequisite**: the `python()` plugin must be enabled both at the
> Eventhouse level **and** on each KQL Database. See
> [`anomaly_detection_fabric_kql.md`](anomaly_detection_fabric_kql.md)
> §2.1.

### 4.3 Training and scoring loops

Two independent loops coexist on the same `anomalies` table, distinguished
by `model_name`. The univariate loop watches one sensor at a time; the
multivariate loop watches all 8 sensors of one machine jointly.

#### Univariate loop

1. `nb_04_train_univariate_ae` reads `raw_telemetry` for one
   `(machine_id, sensor_id)`, builds non-overlapping windows of
   `WINDOW_SIZE` samples, trains a small LSTM autoencoder, exports it to
   ONNX and uploads `(name, version, payload, metadata)` into the `models`
   table. Model name convention: `univariate_ae__<sensor_id>` (e.g.
   `univariate_ae__temperature_motor`).
2. The first update policy on `anomalies` (`fn_score_demo`) calls
   `score_univariate_onnx_batch`, which reads the latest model row and
   scores only the windows that complete in the new ingest batch — see
   [`concepts.md` §9](concepts.md#9-window-based-models-need-a-small-slice-of-history-across-batch-boundaries).
3. For ad-hoc exploration from a notebook or queryset use the lookback
   variant:

   ```kusto
   score_univariate_onnx_lookback('univariate_ae__temperature_motor',
                                  'M-001', 'temperature_motor', 64, 10m, 3870.0)
   | where is_anomaly
   ```

#### Multivariate loop

1. `kql/05_multivariate_mv.kql` creates the materialized view
   `raw_telemetry_wide_mv`: one row per `(machine_id, ts_bin = 1s)` with one
   column per sensor (`temperature_motor`, `temperature_bearing`,
   `vibration_axial`, `vibration_radial`, `current`, `power`, `spindle_rpm`,
   `pressure_hydraulic`). The MV is maintained on every ingest — no batch
   job, ~1× storage overhead, partial bin rows are reconciled automatically.
2. `nb_05_train_multivariate_ae` reads the MV for one machine, builds
   sliding `(WINDOW_SIZE, 8)` windows and trains an LSTM autoencoder
   (encoder/decoder hidden=64, mini-batch SGD, Adam + cosine LR,
   EPOCHS=100, BATCH=32). The exported ONNX is wrapped in a
   `NormalizedScoreWrapper` that **bakes per-feature `mean`/`std` into the
   graph** as constant buffers. This means the KQL scoring function passes
   raw sensor values from the MV — no normalization step in KQL. Output is
   a single MSE score per window across all sensors.
3. The threshold is `mean(loss) + K · std(loss)` on the training set
   (`THRESHOLD_K = 4.0` on normalized features, keeping FPR low) and is
   stored in `metadata.threshold` of the model row. The scoring function
   reads it from there — no hard-coded threshold in KQL.
4. The model is uploaded as `multivariate_ae__<machine_id>` (e.g.
   `multivariate_ae__M-001`).
5. The second update policy on `anomalies` (`fn_score_multivariate_demo`)
   calls `score_multivariate_onnx_batch_from_mv`, which builds windows from
   the wide MV plus a `(window_size − 1)` left-context read for boundary
   straddling, exactly like the univariate path. The function applies an
   **anchor-sensor dedup filter**: it fires only when the new batch contains
   at least one row of `temperature_motor` for the target machine. Without
   this, with 8 sensors per machine the policy would fire ~8× per batch.
6. `pl_retrain` schedules the active training notebooks (04, 05) plus
   `nb_register_kql_scorer`.

### 4.4 Alerting

In Reflex (`act_anomaly_alerts`) connect to the `anomalies` KQL stream and
add a rule — e.g. "send Teams message when a row lands with
`is_anomaly == true` and `score > 4000`".

### 4.5 Validating end-to-end without the simulator

`tools/inject_anomaly.py` appends N contiguous spike samples directly
into `raw_telemetry` for one (machine, sensor). The new extent triggers
the update policy and an anomaly should land in `anomalies` within
~60–90 s. Useful as a smoke test after redeploying the KQL functions or
the model.

Reference smoke tests recorded during deployment of the multivariate model
v3 (M-001, 8 sensors, threshold ≈ 0.388):

- A 64-sample spike of `value = 500` on a single sensor produced score
  ≈ 2620, `is_anomaly = true`, detected in ~50 s.
- The injection is intentionally severe so the test passes regardless of
  jitter; for normal-traffic regression you can lower the spike amplitude
  and verify that scores stay below threshold.

### 4.6 Notebook publishing & runtime dependencies

[`tools/upload_notebook.py`](../tools/upload_notebook.py) uploads a local
`.ipynb` as a Fabric Notebook item, creating it if absent or updating its
definition in place. Display name defaults to `nb_<file stem>` so
`notebooks/05_train_multivariate_ae.ipynb` becomes
`nb_05_train_multivariate_ae` in the workspace.

The Fabric Spark runtime ships a curated package set that does **not**
include `azure-kusto-data`. Notebooks 04 and 05 therefore include a `%pip
install -q azure-kusto-data azure-identity python-dotenv` cell at the very
top. On a local `.venv` it is a no-op; in Fabric it installs the SDK on
first run (kernel restart). For production it is recommended to attach
`env_anomaly` (the Environment item) with these libraries pre-published, in
which case the `%pip` cell can be removed.

## 5. Constraints and trade-offs

For the design rationale see [`concepts.md` §6–10](concepts.md). The
operational constraints to keep in mind:

- **`python()` sandbox**: ~1 GB model size, no internet, packages limited
  to numpy/pandas/scipy/scikit-learn/onnxruntime/statsmodels and a few
  others. ONNX inference fits comfortably; full DL training does not.
- **Update-policy scoring is per-batch**: cost is proportional to the new
  batch plus a tiny `(window_size - 1)` left-context read per (machine,
  sensor). Pin the `ingestionbatching` policy so batches are large enough
  to contain full windows (see `kql/01_tables.kql`).
- **Multivariate alignment**: multivariate windows require sensors to be
  resampled onto a common time bin. The wide materialized view
  `raw_telemetry_wide_mv` does this once on ingest; the scoring function
  forward-fills any missing per-bin values inside the Python sandbox
  (`pandas .ffill().bfill().fillna(0)`) so the model never sees `NaN`.
- **Multivariate dedup**: with N sensors per machine, a naïve update policy
  would fire N times per ingest batch (once per sensor row). The
  `fn_score_multivariate_demo` function uses an *anchor sensor*
  (`temperature_motor`) so it fires ~1× per batch. If the anchor sensor
  ever stops reporting, multivariate scoring stops too — pick a sensor that
  is guaranteed to be present, or move to a routing table for production.
- **Model versioning**: scoring uses `latest_model()` by default. Pin a
  version by changing `score_*_onnx` to take a `version:int` parameter
  if you need staged rollouts.
- **Normalization placement (multivariate)**: per-feature `mean`/`std` are
  baked into the ONNX graph as constant buffers (`NormalizedScoreWrapper`).
  This keeps KQL stateless and removes the risk of train/score skew, but it
  also means a re-fit of the normalization stats requires re-uploading the
  model. Acceptable for periodic retrains; ill-suited to per-batch online
  recalibration.

## 6. Roadmap (not implemented yet)

- Per-(machine, sensor) routing table to drive both update policies at
  scale (today the demo hard-codes `M-001` + `temperature_motor`).
- A/B comparison between models via `version` parameter.
- Real-time dashboard built on top of the KQL database (Semantic Model and
  Report items are provisioned blank).
- Quality-flag handling (`quality < 192`) for sensor dropouts.
- Move the `%pip install` cell out of notebooks 04/05 and into a published
  `env_anomaly` Fabric Environment.
- Promote the multivariate dedup from a single anchor sensor to a small
  helper function that picks the densest sensor per (machine, batch).
