# Architecture — Factory Anomaly Detection on Microsoft Fabric

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
[machines × sensors] ──AMQP/Kafka──► Eventstream  (es-machines)
                                          │
                ┌─────────────────────────┼─────────────────────────┐
                ▼                                                   ▼
  Eventhouse / KQL Database (eh-telemetry / kql-telemetry)    Lakehouse (lh-telemetry)
   ├─ raw_telemetry        (hot store, 30d retention)          ├─ Tables/bronze_telemetry
   ├─ models               (versioned ONNX bytes)              ├─ Tables/silver_telemetry
   ├─ scoring functions    (build window → python(onnx))       ├─ Tables/gold_windows_uni
   ├─ update policy        (auto-score on ingest)              └─ Tables/gold_windows_multi
   └─ anomalies            (detections; 365d retention)                 ▲
                ▲                                                       │
                │                                                       │
   Reflex (act-anomaly-alerts)                            Notebook nb-prepare-features
        Teams / email / pipeline trigger                  Notebook nb-train-export-onnx ──► uploads .onnx into models table
                                                          Notebook nb-register-kql-scorer ──► (re-)applies KQL functions
                                                                  ▲
                                                                  │
                                                Data Pipeline (pl-retrain) — schedule

Semantic Model (sm-anomaly) ◄── KQL DB + Lakehouse gold ──► Report (rpt-anomaly)
```

## 3. Items provisioned by `scripts/deploy.ps1`

| Item             | Name (default)         | Type           | Purpose                                                      |
|------------------|------------------------|----------------|--------------------------------------------------------------|
| Workspace        | `anomaly-detection-dev`| Workspace      | Container; bound to the configured capacity                  |
| Eventstream      | `es-machines`          | Eventstream    | Single ingestion endpoint for all machines (custom-app source); fan-out to KQL + Lakehouse |
| Eventhouse       | `eh-telemetry`         | Eventhouse     | Hosts the KQL database                                       |
| KQL Database     | `kql-telemetry`        | KQLDatabase    | Hot store + ONNX scoring + anomaly table                     |
| Lakehouse        | `lh-telemetry`         | Lakehouse      | Cold store + medallion tables for training                   |
| Environment      | `env-anomaly`          | Environment    | Pinned PySpark libs (torch, onnx, onnxruntime, scikit-learn, skl2onnx, azure-kusto-data) |
| Notebook         | `nb-prepare-features`  | Notebook       | bronze → silver → gold (univariate + multivariate windows)   |
| Notebook         | `nb-train-export-onnx` | Notebook       | Trains a model, exports ONNX, uploads bytes to KQL `models`  |
| Notebook         | `nb-register-kql-scorer`| Notebook      | Reapplies `kql/*.kql` to the database                        |
| Data Pipeline    | `pl-retrain`           | DataPipeline   | Schedules the three notebooks                                |
| Reflex           | `act-anomaly-alerts`   | Reflex         | Reacts on rows landing in the `anomalies` table              |
| Semantic Model   | `sm-anomaly`           | SemanticModel  | Direct Lake model over gold + anomalies                      |
| Report           | `rpt-anomaly`          | Report         | Operational dashboard                                        |

All items are created **blank** (notebooks ship with starter scaffolds in
`items/<name>.Notebook/`). Schema, scoring logic, Eventstream wiring and
Reflex rules are configured separately — see §4.

## 4. Post-deploy configuration

The CLI deploy gives you the empty containers. The shape of the system is
applied next:

### 4.1 Eventstream wiring (portal, one-off)

1. Open `es-machines` → **Add source** → **Custom App**. Note the AMQP /
   Kafka connection string; share it with the device fleet.
2. **Add destination** → **Eventhouse** → select `kql-telemetry`, target
   table `raw_telemetry`, mapping `raw_telemetry_json` (created in §4.2).
3. **Add destination** → **Lakehouse** → `lh-telemetry`, table
   `bronze_telemetry`, Delta format.

Telemetry payload contract (JSON):

```json
{
  "machineId": "M001",
  "sensorId":  "temp",
  "ts":        "2026-05-07T10:15:23.451Z",
  "value":     72.3,
  "quality":   192
}
```

### 4.2 KQL schema

Run the scripts in `kql/` against `kql-telemetry`, in order:

| File                          | Creates                                           |
|-------------------------------|---------------------------------------------------|
| `01_tables.kql`               | `raw_telemetry`, `anomalies`, JSON mapping        |
| `02_models.kql`               | `models` table + `latest_model()` helper          |
| `03_scoring_functions.kql`    | `build_*_windows`, `score_*_onnx` (calls `python()`) |
| `04_update_policy.kql`        | `fn_score_demo` + update policy on `raw_telemetry`|

Two ways to apply them:

- **Portal**: open the KQL queryset attached to the database, paste each file, run.
- **Notebook**: run `nb-register-kql-scorer` (uploads `kql/` to the lakehouse Files area first).

> **Prerequisite**: the `python()` plugin must be enabled on the Eventhouse.
> If it isn't, the scoring functions will compile but fail at execution.

### 4.3 Training and scoring loop

1. `nb-prepare-features` builds `gold_windows_uni` and `gold_windows_multi`.
2. `nb-train-export-onnx` trains a model, exports ONNX, ingests
   `(name, version, payload, …)` into the `models` table.
3. The KQL update policy automatically picks up the latest version on the
   next ingest batch; or you can call the scoring function ad-hoc:

   ```kusto
   score_univariate_onnx('univariate_ae', 'M001', 'temp', 64, 10m, 0.85)
   | where is_anomaly
   ```

4. `pl-retrain` schedules the three notebooks (e.g. nightly).

### 4.4 Alerting

In Reflex (`act-anomaly-alerts`) connect to the `anomalies` KQL stream and
add a rule — e.g. "send Teams message when a row lands with
`is_anomaly == true` and `score > 0.95`".

## 5. Constraints and trade-offs

- **`python()` sandbox**: ~1 GB model size, no internet, packages limited
  to numpy/pandas/scipy/scikit-learn/onnxruntime/statsmodels and a few
  others. ONNX inference fits comfortably; full DL training does not.
- **Window scoring frequency**: update policies run synchronously per
  ingest batch — keep the scoring function fast (<~1–2 s). For wider
  windows or many machines per batch, prefer scheduled scoring (call the
  scoring function from a KQL queryset every N seconds, or from a
  pipeline).
- **Multivariate alignment**: multivariate windows require sensors to be
  resampled onto a common time bin (`build_multivariate_windows` does this
  in KQL; `nb-prepare-features` does the equivalent in Spark for training).
- **Model versioning**: scoring uses `latest_model()` by default. Pin a
  version by changing `score_*_onnx` to take a `version:int` parameter
  if you need staged rollouts.

## 6. Roadmap (not implemented yet)

- Per-(machine, sensor) routing table to drive the update policy at scale.
- A/B comparison between models via `version` parameter.
- Real-time dashboard built on top of the KQL database.
- Quality-flag handling (`quality < 192`) for sensor dropouts.
