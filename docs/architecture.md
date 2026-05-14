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
[machines × sensors] ──AMQP/Kafka──► Eventstream  (es_machines)
                                          │
                ┌─────────────────────────┼─────────────────────────┐
                ▼                                                   ▼
  Eventhouse / KQL Database (eh_telemetry / kql_telemetry)    Lakehouse (lh_telemetry)
   ├─ raw_telemetry        (hot store, 30d retention)          ├─ Tables/bronze_telemetry
   ├─ models               (versioned ONNX bytes)              ├─ Tables/silver_telemetry
   ├─ scoring functions    (build window → python(onnx))       ├─ Tables/gold_windows_uni
   ├─ update policy        (auto-score on ingest)              └─ Tables/gold_windows_multi
   └─ anomalies            (detections; 365d retention)                 ▲
                ▲                                                       │
                │                                                       │
   Reflex (act_anomaly_alerts)                            Notebook nb_prepare_features
        Teams / email / pipeline trigger                  Notebook nb_train_export_onnx ──► uploads .onnx into models table
                                                          Notebook nb_register_kql_scorer ──► (re-)applies KQL functions
                                                                  ▲
                                                                  │
                                                Data Pipeline (pl_retrain) — schedule

Semantic Model (sm_anomaly) ◄── KQL DB + Lakehouse gold ──► Report (rpt_anomaly)
```

## 3. Items provisioned by `scripts/deploy.ps1`

| Item             | Name (default)         | Type           | Purpose                                                      |
|------------------|------------------------|----------------|--------------------------------------------------------------|
| Workspace        | `anomaly-detection-dev`| Workspace      | Container; bound to the configured capacity                  |
| Eventstream      | `es_machines`          | Eventstream    | Single ingestion endpoint for all machines (custom-app source); fan-out to KQL + Lakehouse |
| Eventhouse       | `eh_telemetry`         | Eventhouse     | Hosts the KQL database                                       |
| KQL Database     | `kql_telemetry`        | KQLDatabase    | Hot store + ONNX scoring + anomaly table                     |
| Lakehouse        | `lh_telemetry`         | Lakehouse      | Cold store + medallion tables for training                   |
| Environment      | `env_anomaly`          | Environment    | Pinned PySpark libs (torch, onnx, onnxruntime, scikit-learn, skl2onnx, azure-kusto-data) |
| Notebook         | `nb_prepare_features`  | Notebook       | bronze → silver → gold (univariate + multivariate windows)   |
| Notebook         | `nb_train_export_onnx` | Notebook       | Trains a model, exports ONNX, uploads bytes to KQL `models`  |
| Notebook         | `nb_register_kql_scorer`| Notebook      | Reapplies `kql/*.kql` to the database                        |
| Data Pipeline    | `pl_retrain`           | DataPipeline   | Schedules the three notebooks                                |
| Reflex           | `act_anomaly_alerts`   | Reflex         | Reacts on rows landing in the `anomalies` table              |
| Semantic Model   | `sm_anomaly`           | SemanticModel  | Direct Lake model over gold + anomalies                      |
| Report           | `rpt_anomaly`          | Report         | Operational dashboard                                        |

All items are created **blank** (notebooks ship with starter scaffolds in
`items/<name>.Notebook/`). Schema, scoring logic, Eventstream wiring and
Reflex rules are configured separately — see §4. Item names use
underscores because some Fabric item types (Eventstream, Reflex, …)
reject hyphens; the defaults are defined in `.env.example` and can be
overridden in `.env`.

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
| `01_tables.kql`               | `raw_telemetry`, `anomalies`, JSON mapping, batching policy |
| `02_models.kql`               | `models` table + `latest_model()` helper          |
| `03_scoring_functions.kql`    | `score_univariate_onnx_batch` (update-policy safe) and `score_univariate_onnx_lookback` (ad-hoc) |
| `04_update_policy.kql`        | `fn_score_demo` + update policy on `raw_telemetry`|

Two ways to apply them:

- **Portal**: open the KQL queryset attached to the database, paste each file, run.
- **Script**: run `python tools/02_setup_kql_tables.py kql/*.kql` (auth via
  the same `.env` used for `scripts/deploy.ps1`).
- **Notebook**: run `nb_register_kql_scorer` (uploads `kql/` to the lakehouse Files area first).

> **Prerequisite**: the `python()` plugin must be enabled both at the
> Eventhouse level **and** on each KQL Database. See
> [`../anomaly_detection_fabric_kql.md`](../anomaly_detection_fabric_kql.md)
> §2.1.

### 4.3 Training and scoring loop

1. `nb_prepare_features` builds `gold_windows_uni` and `gold_windows_multi`.
2. `nb_train_export_onnx` trains a model, exports ONNX, ingests
   `(name, version, payload, …)` into the `models` table. Model names follow
   the convention `univariate_ae__<sensor_id>` (e.g.
   `univariate_ae__temperature_motor`).
3. The KQL update policy automatically picks up the latest version on the
   next ingest batch via `score_univariate_onnx_batch` (see
   [`concepts.md` §9](concepts.md#9-window-based-models-need-a-small-slice-of-history-across-batch-boundaries)
   for why this function is the one safe to call from an update policy).
   For ad-hoc exploration from a notebook or queryset use the lookback
   variant:

   ```kusto
   score_univariate_onnx_lookback('univariate_ae__temperature_motor',
                                  'M-001', 'temperature_motor', 64, 10m, 3870.0)
   | where is_anomaly
   ```

4. `pl_retrain` schedules the three notebooks (e.g. nightly).

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
  resampled onto a common time bin (`build_multivariate_windows` does this
  in KQL; `nb_prepare_features` does the equivalent in Spark for training).
- **Model versioning**: scoring uses `latest_model()` by default. Pin a
  version by changing `score_*_onnx` to take a `version:int` parameter
  if you need staged rollouts.

## 6. Roadmap (not implemented yet)

- Per-(machine, sensor) routing table to drive the update policy at scale.
- A/B comparison between models via `version` parameter.
- Real-time dashboard built on top of the KQL database.
- Quality-flag handling (`quality < 192`) for sensor dropouts.
