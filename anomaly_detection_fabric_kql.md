# Anomaly Detection in Fabric Eventhouse — Complete KQL guide

This guide covers every available path for doing anomaly detection **inside Fabric Eventhouse**, with near real-time scoring, leveraging:

1. The **native KQL time-series functions** (the simplest path, often sufficient on its own)
2. The **`python()` plugin** with custom models serialized inside the same Eventhouse
3. A combination of the two, plus **update policies** + **Activator**, to close the alerting loop

---

## 1. Reference architecture

The end-to-end flow is the same regardless of the algorithm:

```
┌──────────────┐    ┌────────────┐    ┌──────────────────┐    ┌──────────────┐    ┌───────────┐
│ Machines     │───▶│ Eventstream│───▶│ measures_raw     │───▶│ anomalies    │───▶│ Activator │
│ OPC-UA / MQTT│    │            │    │ (Eventhouse)     │    │ (Eventhouse) │    │ (Reflex)  │
└──────────────┘    └────────────┘    │  + update policy │    └──────────────┘    └───────────┘
                                      └──────────────────┘                              │
                                                                                        ▼
                                                                              Email / Teams / Webhook
```

**Components:**

- **Eventstream** — brings process data into Eventhouse (Event Hubs, IoT Hub, MQTT, Kafka, custom app).
- **`measures_raw`** — landing table in Eventhouse with raw samples.
- **Update policy** — KQL function that, on each new ingested batch, applies the model and writes only anomalous rows into `anomalies`.
- **`anomalies`** — table of detected anomalies (model input/output).
- **Activator (Reflex)** — watches `anomalies` and fires notifications.

---

## 2. Prerequisites and initial setup

### 2.1 Enabling the Python plugin on the Eventhouse

Required only for custom scenarios (not for native functions). The plugin is **disabled by default** and must be turned on by the Eventhouse administrator:

`Eventhouse > Plugins > Python language extension: On`

Available images today:

- **Python 3.10.8** + standard data-science / ML packages (numpy, pandas, scikit-learn, statsmodels, scipy, …)
- **Python 3.11.7** same as above
- **Python 3.11.7 DL** + tensorflow + torch + `time-series-anomaly-detector` (required for multivariate MVAD anomaly detection)

> ⚠️ Enabling a plugin causes a **hot-cache refresh** of the Eventhouse, which can take up to one hour. Do it during a low-load window.

### 2.2 Base tables

```kusto
.create table measures_raw (
    machine_id: string,
    ts: datetime,
    temp: real,
    vib: real,
    press: real,
    rpm: real
)

.create table anomalies (
    machine_id: string,
    ts: datetime,
    temp: real,
    vib: real,
    press: real,
    rpm: real,
    score: real,
    is_anomaly: bool,
    model_version: string,
    detected_at: datetime
)

.create table models (
    name: string,
    version: string,
    created_at: datetime,
    model: string,           // pickle serialized as base64
    features: dynamic,       // feature list
    metadata: dynamic        // metrics, training info
)
```

### 2.3 Streaming vs queued ingestion (IMPORTANT constraint)

The Python plugin **does not work inside update policies fed by streaming ingestion**. It only works with:

- **Queued (batch) ingestion** on the source table
- `.set-or-append` from a query

> **Heads-up — in Microsoft Fabric Eventhouse, streaming ingestion is _enabled by default_ on every newly created table.** This means an Eventstream attached to a fresh table will use streaming, and any update policy that calls `python()` on that table will silently fail to fire (or be rejected at attach time).
>
> You must explicitly **disable streaming ingestion on the source table** before attaching a Python-based update policy. One-shot KQL command:
>
> ```kql
> .alter table raw_telemetry policy streamingingestion '{"IsEnabled": false}'
> ```
>
> After this, Eventstream delivers data via queued ingestion (typical latency 5-30 s — perfectly fine for industrial anomaly detection). Sources:
> - <https://learn.microsoft.com/kusto/query/python-plugin?view=microsoft-fabric#use-ingestion-from-query-and-update-policy>
> - <https://learn.microsoft.com/azure/data-explorer/ingest-data-streaming#limitations>

For pure-streaming use cases (sub-second latency required), scoring must be done in a **two-stage pipeline**: streaming table → batch follower table (with the update policy + Python).

---

## 3. Path A — Native KQL functions only (the simplest)

The native functions perform **seasonal decomposition + outlier analysis on residuals** entirely in-engine, vectorized, across thousands of series in parallel. No Python sandbox, no models to manage.

### 3.1 Key functions

| Function | Purpose |
|---|---|
| `make-series` | Builds time-aligned arrays bucketed on time bins |
| `series_decompose()` | Decomposes into `baseline` (seasonal+trend), `seasonal`, `trend`, `residual` |
| `series_decompose_anomalies()` | Decomposition + anomaly flagging on residuals (Tukey test) |
| `series_decompose_forecast()` | Forecast extrapolating seasonal+trend |
| `series_outliers()` | Outlier detection on a generic series (Tukey) |
| `series_periods_detect()` | Detects the seasonality of a series |

### 3.2 `series_decompose_anomalies` syntax

```
series_decompose_anomalies(Series, [Threshold, Seasonality, Trend, Test_points, AD_method, Seasonality_threshold])
```

| Parameter | Default | Meaning |
|---|---|---|
| `Threshold` | `1.5` | Sensitivity (higher = fewer anomalies) |
| `Seasonality` | `-1` | `-1` = auto-detect; `0` = none; integer = number of bins per cycle |
| `Trend` | `'avg'` | `'avg'` (mean only), `'linefit'` (linear regression), `'none'` |
| `Test_points` | `0` | Trailing points to exclude from training |
| `AD_method` | `'ctukey'` | `'ctukey'` (clipped Tukey) or `'tukey'` |
| `Seasonality_threshold` | `0.6` | Score threshold for auto-seasonality |

It returns **three series** aligned to the input:

- `ad_flag` — ternary: `+1` (spike), `-1` (dip), `0` (normal)
- `ad_score` — continuous anomaly score (higher = more anomalous)
- `baseline` — the expected curve (useful to visualize the "deviation")

### 3.3 Example: anomaly detection across all machines

```kusto
let lookback   = 7d;
let bin_size   = 1m;
let threshold  = 2.5;
let last_only  = 5m;     // "live" window of interest
//
measures_raw
| where ts > ago(lookback)
| make-series 
    avg_temp = avg(temp), 
    avg_vib  = avg(vib), 
    avg_press = avg(press)
    on ts from ago(lookback) to now() step bin_size 
    by machine_id
| extend (anomaly_temp,  score_temp,  baseline_temp)  = series_decompose_anomalies(avg_temp,  threshold, -1, 'linefit')
| extend (anomaly_vib,   score_vib,   baseline_vib)   = series_decompose_anomalies(avg_vib,   threshold, -1, 'linefit')
| extend (anomaly_press, score_press, baseline_press) = series_decompose_anomalies(avg_press, threshold, -1, 'linefit')
| mv-expand 
    ts to typeof(datetime),
    avg_temp to typeof(real),     anomaly_temp  to typeof(int), score_temp  to typeof(real), baseline_temp  to typeof(real),
    avg_vib  to typeof(real),     anomaly_vib   to typeof(int), score_vib   to typeof(real), baseline_vib   to typeof(real),
    avg_press to typeof(real),    anomaly_press to typeof(int), score_press to typeof(real), baseline_press to typeof(real)
| where ts > ago(last_only)
| where anomaly_temp != 0 or anomaly_vib != 0 or anomaly_press != 0
| project ts, machine_id, 
          avg_temp, baseline_temp, anomaly_temp, score_temp,
          avg_vib,  baseline_vib,  anomaly_vib,  score_vib,
          avg_press,baseline_press,anomaly_press,score_press
```

The `make-series … | series_decompose_anomalies | mv-expand` pattern is the classic use case. It works very well for **univariate anomalies**, one metric at a time, optionally combined with OR/AND.

### 3.4 When to use it

| Scenario | Suitable? |
|---|---|
| One/few metrics per machine, value-based anomalies | ✅ |
| Seasonal patterns (shifts, weekly, machine cycle) | ✅ with `linefit` + auto-seasonality |
| Hundreds/thousands of series | ✅ it is vectorized |
| "Multivariate" anomalies (cross-sensor correlation) | ❌ one at a time, doesn't see correlations |
| Complex patterns (anomalous waveforms) | ❌ requires a custom model |

### 3.5 Wrapping it in an update policy

To enable near real-time, turn the query into a **function** + **update policy**:

```kusto
.create-or-alter function with (folder='ml', skipvalidation='true')
DetectAnomaliesNative() {
    let bin_size  = 1m;
    let threshold = 2.5;
    let lookback  = 2h;
    measures_raw
    | where ts > ago(lookback)
    | make-series avg_temp = avg(temp) on ts from ago(lookback) to now() step bin_size by machine_id
    | extend (ad_flag, ad_score, baseline) = series_decompose_anomalies(avg_temp, threshold, -1, 'linefit')
    | mv-expand ts to typeof(datetime), 
                avg_temp to typeof(real), 
                ad_flag to typeof(int), 
                ad_score to typeof(real), 
                baseline to typeof(real)
    | where ad_flag != 0 and ts > ago(bin_size * 2)   // last bin only
    | project machine_id, ts, 
              temp = avg_temp, vib = real(null), press = real(null), rpm = real(null),
              score = ad_score, is_anomaly = true,
              model_version = "native_v1", detected_at = now()
}
```

```kusto
.alter table anomalies policy update 
@'[{"IsEnabled": true, 
   "Source": "measures_raw", 
   "Query": "DetectAnomaliesNative()", 
   "IsTransactional": false, 
   "PropagateIngestionProperties": false}]'
```

From now on every new batch into `measures_raw` triggers the function, and anomalous rows land in `anomalies`.

> ⚠️ An update policy with `make-series` over a lookback window is slightly more expensive than one operating on a single record because it rebuilds the series on each trigger. For high throughput (>10k events/sec) the Python pattern with pre-loaded state is preferable.

---

## 4. Path B — Custom Python model via the `python()` plugin

This is the path for models that are not expressible as seasonal decomposition: Isolation Forest, One-Class SVM, autoencoders, gradient boosting, etc.

### 4.1 How the Python plugin works

```kusto
T 
| evaluate python(
    typeof(*, score:real, is_anomaly:bool),   // output schema
    'python_code_string',                      // code
    bag_pack('param1', value1, ...),           // kargs dictionary
    external_artifacts                         // optional, files from blob
)
```

**Reserved variables:**

- `df` — pandas DataFrame containing the input data (the rows arriving from the pipe)
- `kargs` — dictionary with parameters passed via `bag_pack`
- `result` — pandas DataFrame with the output (must match the declared schema)

**Sandbox limits to keep in mind:**

- Limited memory (order of GB, depends on Eventhouse SKU)
- No arbitrary network access
- Packages must be in the image, or shipped via `external_artifacts` zip
- Short timeout (tens of seconds)

### 4.2 Pattern: model stored inside Eventhouse

Standard schema for real-time scoring:

1. **Offline training** (Fabric Spark notebook): reads history, trains, **serializes the model as pickle**, base64-encodes it, and writes a row to the `models` table.
2. **Scoring function** in KQL reads the latest model from the table and passes it to the Python plugin via `kargs`, scoring the batch.
3. **Update policy** on the raw table calls the function and writes into `anomalies`.

#### 4.2.1 Training notebook (Fabric Spark)

```python
import mlflow
import pickle, base64, json
from datetime import datetime
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

# 1) Read history via OneLake (Eventhouse → Delta on OneLake is on-demand)
abfss_uri = "abfss://<workspace>@onelake.dfs.fabric.microsoft.com/<eventhouse>.KQL/Tables/measures_raw"
df = spark.read.format("delta").load(abfss_uri).toPandas()

# 2) Filter "normal" period and features
features = ["temp", "vib", "press", "rpm"]
df_train = df[df["ts"] < "2026-04-01"]  # nominal period
X = df_train[features].values

# 3) Pipeline: scaler + isolation forest
pipe = Pipeline([
    ("scaler", StandardScaler()),
    ("iforest", IsolationForest(
        n_estimators=200,
        contamination=0.01,
        random_state=42,
        n_jobs=-1
    ))
])
pipe.fit(X)

# 4) Log to MLflow for lifecycle
with mlflow.start_run() as run:
    mlflow.sklearn.log_model(pipe, "iforest")
    mlflow.log_params({"n_estimators": 200, "contamination": 0.01})
    mlflow.log_metric("n_train_samples", len(X))
    run_id = run.info.run_id

# 5) Serialize for Eventhouse
model_bytes = pickle.dumps(pipe)
model_b64 = base64.b64encode(model_bytes).decode("ascii")
print(f"Model size: {len(model_bytes)/1024:.1f} KB")  # keep an eye < 5-10 MB

# 6) Push into the `models` table via Kqlmagic / Kusto SDK
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.kusto.ingest import QueuedIngestClient, IngestionProperties, DataFormat

cluster_uri = "https://<eventhouse>.kusto.fabric.microsoft.com"
db = "<db_name>"

token = mssparkutils.credentials.getToken(cluster_uri)
kcsb = KustoConnectionStringBuilder.with_aad_user_token_authentication(cluster_uri, token)

import pandas as pd
row = pd.DataFrame([{
    "name": "iforest_v1",
    "version": run_id,
    "created_at": datetime.utcnow(),
    "model": model_b64,
    "features": json.dumps(features),
    "metadata": json.dumps({"n_estimators": 200, "contamination": 0.01})
}])

# Ingest into models table
ingest_client = QueuedIngestClient(kcsb)
props = IngestionProperties(database=db, table="models", data_format=DataFormat.CSV)
ingest_client.ingest_from_dataframe(row, ingestion_properties=props)
```

#### 4.2.2 KQL scoring function

```kusto
.create-or-alter function with (folder='ml', skipvalidation='true')
ScoreWithIForest(samples: (machine_id:string, ts:datetime, temp:real, vib:real, press:real, rpm:real)) {
    let model_row = toscalar(
        models 
        | where name == "iforest_v1" 
        | top 1 by created_at desc 
        | project pack('model_b64', model, 'features', features, 'version', version)
    );
    let model_b64    = tostring(model_row.model_b64);
    let features_arr = todynamic(model_row.features);
    let model_ver    = tostring(model_row.version);
    samples
    | evaluate python(
        typeof(*, score:real, is_anomaly:bool, model_version:string, detected_at:datetime),
        ```
import pickle, base64
import pandas as pd
from datetime import datetime

model = pickle.loads(base64.b64decode(kargs["model_b64"]))
features = list(kargs["features"])

X = df[features].values
# decision_function: higher = more normal (sklearn convention)
scores = model.decision_function(X)
preds  = model.predict(X)   # -1 anomalous, +1 normal

result = df.copy()
result["score"] = -scores                  # inverted: higher = more anomalous
result["is_anomaly"] = (preds == -1)
result["model_version"] = kargs["version"]
result["detected_at"] = datetime.utcnow()
        ```,
        bag_pack('model_b64', model_b64, 'features', features_arr, 'version', model_ver)
    )
}
```

#### 4.2.3 Update policy

```kusto
.alter table anomalies policy update 
@'[{"IsEnabled": true, 
   "Source": "measures_raw", 
   "Query": "ScoreWithIForest(measures_raw) | where is_anomaly", 
   "IsTransactional": false, 
   "PropagateIngestionProperties": false}]'
```

Every new record entering `measures_raw` is scored; only anomalous rows land in `anomalies`. Typical latency is a few seconds from ingestion.

> **Two gotchas to remember when wiring this up:**
>
> 1. **Disable streaming on the source table first** — see §2.3. Without it the plugin invocation fails silently.
> 2. **Per-extent rewrite of the source table.** When invoked from an update policy the engine rewrites every direct reference to the source table as `__table("measures_raw") | where extent_id() in (guid(<just-ingested-extent>))`. That means the function only sees the rows of the new extent — typically a tiny slice (seconds to a couple of minutes). For point-wise scoring (one row in, one score out) that's fine. For **window-based scoring** (e.g. 64-sample autoencoder windows) a single extent rarely contains enough contiguous samples per `(machine, sensor)` to build any complete window, so nothing comes out and the `anomalies` table stays empty.
>
>     Workaround: inside the scoring function, reference the source table **indirectly via `database()`** so the engine does NOT apply the extent filter and you can read the last N minutes:
>
>     ```kusto
>     // INSIDE the function called by the update policy
>     database(current_database()).measures_raw
>     | where ts > now() - 10m
>     | ...
>     ```
>
>     The trade-off is that every batch re-reads the full lookback and re-emits the same windows → you'll get **duplicate anomaly rows**. Mitigations:
>     - Dedupe at query time (`anomalies | summarize arg_max(detected_at, *) by window_start, machine_id, sensor_id`).
>     - Or persist a `(window_start, machine_id, sensor_id)` "already-emitted" tag and filter inside the function.
>     - Or make the function emit only windows whose `window_end` is within the new batch's timestamp range.

### 4.3 Pattern: time-series feature engineering

Often the anomaly is not on a single point but on the signal shape (rolling stats, delta, derivative, FFT). Two paths:

**Option A — feature engineering inside KQL before the plugin**

```kusto
.create-or-alter function ScoreWithFeaturesV1(window_minutes:int = 5) {
    measures_raw
    | where ts > ago(2 * window_minutes * 1m)
    | partition hint.strategy=native by machine_id (
        order by ts asc
        | extend 
            temp_roll_mean  = row_window_session(temp,  window_minutes * 1m, 1m, ts != prev(ts)),
            temp_roll_std   = todouble(0),  // computed inside Python
            temp_delta      = temp - prev(temp, 1)
    )
    | invoke ScoreWithIForest()
}
```

**Option B — feature engineering inside Python**

More flexible, but the sandbox sees only the rows of the current batch. For long windows you need **lookback data in the batch**, so the query passes more context than the bare minimum.

```kusto
let lookback_min = 5m;
measures_raw
| where ts > ago(lookback_min * 3)
| evaluate python(
    typeof(*, score:real, is_anomaly:bool),
    ```
import pandas as pd, numpy as np, pickle, base64

model = pickle.loads(base64.b64decode(kargs["model_b64"]))
df = df.sort_values(["machine_id", "ts"])

# rolling features per machine
g = df.groupby("machine_id")
df["temp_mean5"]  = g["temp"].transform(lambda s: s.rolling(5, min_periods=1).mean())
df["temp_std5"]   = g["temp"].transform(lambda s: s.rolling(5, min_periods=1).std().fillna(0))
df["vib_mean5"]   = g["vib"].transform(lambda s: s.rolling(5, min_periods=1).mean())
df["temp_delta"]  = g["temp"].diff().fillna(0)

feat = ["temp", "vib", "press", "rpm", "temp_mean5", "temp_std5", "vib_mean5", "temp_delta"]
X = df[feat].fillna(0).values

df["score"] = -model.decision_function(X)
df["is_anomaly"] = model.predict(X) == -1

# Keep only "fresh" records (the others were lookback)
result = df[df["ts"] >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=2)].copy()
    ```,
    bag_pack('model_b64', toscalar(models | top 1 by created_at desc | project model))
)
```

### 4.4 Pattern: heavy model via `external_artifacts`

If the pickle exceeds a few MB (autoencoders, ONNX models), it is preferable **not** to store it in the table but in a blob/Lakehouse and reference it as an external artifact:

```kusto
samples
| evaluate python(
    typeof(*, score:real, is_anomaly:bool),
    ```
import pickle
with open(r"C:\Temp\autoencoder.pkl", "rb") as f:
    model = pickle.load(f)
# ... rest of the scoring
    ```,
    bag_pack(),
    external_artifacts = dynamic({
        "autoencoder.pkl": "https://<lakehouse>/<path>/autoencoder.pkl?<sas>"
    })
)
```

The file is downloaded into the sandbox and made available at `C:\Temp\<name>`.

### 4.5 Pattern: ONNX (recommended for heavy / cross-framework models)

ONNX is particularly well suited because:

- Inference is faster than native sklearn
- Independent of the training framework (PyTorch, TF, sklearn)
- Compact file, fast deserialization

```python
# In the training notebook: convert sklearn → ONNX
from skl2onnx import to_onnx
onx = to_onnx(pipe, X[:1].astype(np.float32))
with open("/lakehouse/default/Files/iforest.onnx", "wb") as f:
    f.write(onx.SerializeToString())
```

In KQL, use `external_artifacts` pointing at the ONNX file and load it with `onnxruntime` inside the sandbox.

---

## 5. Path C — Managed multivariate (preview): `series_mv_*` + `time-series-anomaly-detector`

Microsoft has shipped a managed multivariate capability that internally uses a Microsoft Research model. You must enable the **Python 3.11.7 DL** plugin, which includes the `time-series-anomaly-detector` package.

The approach:

1. Training in a Spark notebook with the package, model saved to MLflow
2. Model path (ABFSS) used in KQL as `external_artifacts`
3. A scoring function invokes the model on the incoming batch

This is the most "no-code-side" path if the use case is classic (cross-sensor correlation, process drift). Constraint: it consumes more resources and requires the DL image (lock-in on the SKU choice).

---

## 6. Update policies — mechanics and best practices

### 6.1 Anatomy

```json
{
  "IsEnabled": true,
  "Source": "measures_raw",                  // input table
  "Query": "ScoreWithIForest(measures_raw)", // function that produces the rows to write
  "IsTransactional": false,                  // if true, failure = ingestion rollback
  "PropagateIngestionProperties": false      // propagates tags/metadata from ingestion
}
```

### 6.2 Things to watch out for

- **`IsTransactional`** — set to `false` for ML scoring: if the model fails, the raw row must still be persisted. Set to `true` only if you'd rather lose the raw datum than not score it.
- **Stateful functions (lookup of the `models` table)** — the query is re-executed on every batch, so the `toscalar(models | ...)` is re-evaluated each time. That's OK because you keep a single "latest" row and the cost is negligible.
- **Cascading update policies** — you can have policies that read from `anomalies` and write to `anomalies_aggregated`, with rules like "if a repeated anomaly within window X then a higher-severity alarm".
- **Idempotency** — if you re-ingest the same file the update policy runs again. To avoid duplicates in `anomalies`, include a natural key + periodic dedup, or enable a `materialized-view` with `arg_max` as a consolidation pattern.

### 6.3 Verification and debugging

```kusto
// Update policy state
.show table anomalies policy update

// Update policy failures in the last hours
.show ingestion failures
| where Table == "anomalies" and FailedOn > ago(1h)

// Audit what the function would produce WITHOUT writing it
ScoreWithIForest(measures_raw | where ts > ago(5m)) | take 100
```

---

## 7. Activator (Reflex) — closing the loop on notifications

Once `anomalies` is being populated, the **Activator** is the piece that fires the alert.

### 7.1 Two modes

**A. Eventstream → Activator (sub-second)**

Add the Activator as a destination of the Eventstream itself, or attach the Activator to an Eventstream that reads from `anomalies` (via OneLake availability + KQL DB source). Latency: under a second. Suitable for "any anomaly → notify" alerts.

**B. KQL Queryset trigger on `anomalies` (poll, ~minutes)**

The Activator periodically (e.g. every 1-5 min) runs a KQL query that returns the anomalies to notify. This allows complex logic: "at least N anomalies on the same machine in 10 minutes", "anomaly on temp followed by anomaly on vib within 2 min", etc. Latency: equal to the poll period.

### 7.2 Example trigger query with anti-flood

```kusto
let window = 10m;
let min_consecutive = 3;
anomalies
| where detected_at > ago(window)
| summarize 
    n = count(), 
    last_score = max(score), 
    last_ts = max(ts) 
    by machine_id, model_version
| where n >= min_consecutive
| project machine_id, n_anomalies = n, last_score, last_ts
```

On the output of this query the Activator can configure:

- Condition: `n_anomalies >= 3`
- Action: email / Teams / Power Automate / custom webhook / run a Fabric pipeline or notebook

### 7.3 Caveats
- The Activator must poll more frequently than the query window, otherwise alerts may be lost.
- It is not "exactly-once": if two cycles overlap inside the window, it can duplicate. For anomalies that's usually acceptable (better double than missed), but if you need dedup, use a separate state table.

---

## 8. Operations — retraining, drift, versioning

### 8.1 Scheduled retraining

A **Fabric Pipeline** that every N days:

1. Runs the training notebook
2. The notebook writes a new row into `models` with a new `version`
3. The scoring function automatically picks the latest (`top 1 by created_at desc`)

Nothing to change in KQL: the deploy is "the model is in the table".

### 8.2 Safe versioning (canary)

Keep an `is_active` column in the `models` table and a function that reads only models with `is_active == true`. To validate a new model:

1. Insert v2 with `is_active = false`
2. Create a `ScoreCanary` scoring function that explicitly uses `version == "v2"` and writes to `anomalies_canary`
3. Compare v1 vs v2 on live data for a few days
4. When OK, swap `is_active`

### 8.3 Drift monitoring

Another update policy / scheduled job that compares the recent feature distribution against the training one:

```kusto
.create-or-alter function MonitorDrift() {
    let train_stats = toscalar(models | top 1 by created_at desc | project metadata.feature_stats);
    measures_raw
    | where ts > ago(24h)
    | summarize 
        temp_mean = avg(temp), temp_std = stdev(temp),
        vib_mean  = avg(vib),  vib_std  = stdev(vib)
        by machine_id
    | extend drift_score = abs(temp_mean - todouble(train_stats.temp_mean)) / todouble(train_stats.temp_std)
    | where drift_score > 3
}
```

Output → `drift_alerts` table → another Activator. Without this, in industrial production, after a few months you typically start getting false positives/negatives without noticing.

### 8.4 Eventhouse monitoring

Enabling Fabric **Workspace Monitoring** gives you the `EventhouseCommandLogs`, `EventhouseDataOperations`, `EventhouseIngestionResultLogs` tables, where you can see:

- Update policy execution latency
- Scoring failures (e.g. incompatible model, sandbox OOM)
- Ingestion volume vs scoring

From these you can build additional Activators for "the model is failing".

---

## 9. Decision summary — which path for which case

| Case | Recommended path |
|---|---|
| One metric per machine, clear seasonal pattern | **A** — `series_decompose_anomalies` |
| Many independent metrics, value-based anomalies | **A** repeated for each metric |
| Cross-sensor correlation, custom model | **B** — Isolation Forest in pickle + update policy |
| Shape patterns (waveforms, vibrations) | **B** — autoencoder via ONNX/external_artifacts |
| Classic managed multivariate | **C** — `time-series-anomaly-detector` |
| Very heavy model (large DL) | External endpoint (Azure ML) — not covered here |

Operational advice: **start with A** to have a baseline in production within days, then complement with **B** for cases A doesn't cover. The two paths coexist nicely: same `anomalies` table, `model_version` column distinguishing who produced what.

---

## 10. Implementation checklist

- [ ] Eventhouse created, OneLake availability ON
- [ ] `measures_raw`, `anomalies`, `models` tables created with the final schema
- [ ] Eventstream attached to `measures_raw` (queued ingestion)
- [ ] Python plugin enabled (3.11.7 or DL if needed)
- [ ] Native KQL function `DetectAnomaliesNative` created and tested on history
- [ ] Training notebook that writes into `models` (and MLflow)
- [ ] `ScoreWithIForest` function tested in "ad-hoc" mode on historical batches
- [ ] Update policy enabled on `anomalies` with `IsTransactional=false`
- [ ] Activator attached to `anomalies` with anti-flood
- [ ] Scheduled retraining pipeline (weekly/monthly)
- [ ] Drift monitoring + tier-2 Activator
- [ ] Workspace monitoring enabled for operational visibility
