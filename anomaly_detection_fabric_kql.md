# Anomaly Detection in Fabric Eventhouse — Guida completa con KQL

Questa guida copre tutte le strade per fare anomaly detection **dentro Fabric Eventhouse**, con scoring quasi real-time, sfruttando:

1. Le **funzioni native KQL** per time series (la via più semplice e spesso sufficiente)
2. Il **plugin `python()`** con modelli custom serializzati nella stessa Eventhouse
3. La combinazione dei due con **update policy** + **Activator** per chiudere il loop di alerting

---

## 1. Architettura di riferimento

Il flusso end-to-end che useremo è sempre lo stesso, indipendentemente dall'algoritmo:

```
┌──────────────┐    ┌────────────┐    ┌──────────────────┐    ┌──────────────┐    ┌───────────┐
│ Macchine     │───▶│ Eventstream│───▶│ measures_raw     │───▶│ anomalies    │───▶│ Activator │
│ OPC-UA / MQTT│    │            │    │ (Eventhouse)     │    │ (Eventhouse) │    │ (Reflex)  │
└──────────────┘    └────────────┘    │  + update policy │    └──────────────┘    └───────────┘
                                      └──────────────────┘                              │
                                                                                        ▼
                                                                              Email / Teams / Webhook
```

**Componenti:**

- **Eventstream** — porta dentro Eventhouse i dati di processo (Event Hubs, IoT Hub, MQTT, Kafka, custom app).
- **`measures_raw`** — tabella di atterraggio in Eventhouse con i sample grezzi.
- **Update policy** — funzione KQL che, ad ogni nuovo batch ingerito, applica il modello e scrive in `anomalies` solo i record anomali.
- **`anomalies`** — tabella delle anomalie rilevate (input/output del modello).
- **Activator (Reflex)** — osserva `anomalies` e fa partire le notifiche.

---

## 2. Prerequisiti e setup iniziale

### 2.1 Abilitare il plugin Python sull'Eventhouse

Necessario solo per gli scenari custom (non per le funzioni native). Il plugin è **disabilitato di default** e va attivato dall'amministratore dell'Eventhouse:

`Eventhouse > Plugins > Python language extension: On`

Le immagini disponibili oggi:

- **Python 3.10.8** + pacchetti data science / ML standard (numpy, pandas, scikit-learn, statsmodels, scipy…)
- **Python 3.11.7** stessa cosa
- **Python 3.11.7 DL** + tensorflow + torch + `time-series-anomaly-detector` (necessario per anomaly detection multivariata MVAD)

> ⚠️ Abilitare un plugin causa un **refresh della cache hot** dell'Eventhouse, che può richiedere fino a un'ora. Conviene farlo durante un periodo di basso carico.

### 2.2 Tabelle di base

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
    model: string,           // pickle serializzato in base64
    features: dynamic,       // lista feature
    metadata: dynamic        // metriche, info di training
)
```

### 2.3 Streaming vs queued ingestion (vincolo IMPORTANTE)

Il plugin Python **non funziona dentro update policy alimentate da streaming ingestion**. Funziona solo con:

- Queued (batch) ingestion — il default per Eventstream
- `.set-or-append` da query

Se l'Eventstream è configurato in modalità streaming pura, lo scoring va fatto in una **tabella intermedia** alimentata in batch. Per la maggior parte dei casi industriali (sample ogni N secondi/minuti) la queued ingestion basta e avanza con latenze tipiche di 5-30 secondi.

---

## 3. Strada A — Solo funzioni native KQL (la più semplice)

Le funzioni native fanno **decomposizione stagionale + analisi degli outlier sui residui** completamente in-engine, vettorializzate, su migliaia di serie in parallelo. Niente sandbox Python, niente modelli da gestire.

### 3.1 Funzioni chiave

| Funzione | A cosa serve |
|---|---|
| `make-series` | Costruisce array temporali allineati su bin temporali |
| `series_decompose()` | Scompone in `baseline` (seasonal+trend), `seasonal`, `trend`, `residual` |
| `series_decompose_anomalies()` | Decomposizione + flag anomalie sui residui (Tukey test) |
| `series_decompose_forecast()` | Forecast estrapolando seasonal+trend |
| `series_outliers()` | Outlier detection su una serie generica (Tukey) |
| `series_periods_detect()` | Rileva la stagionalità di una serie |

### 3.2 Sintassi `series_decompose_anomalies`

```
series_decompose_anomalies(Series, [Threshold, Seasonality, Trend, Test_points, AD_method, Seasonality_threshold])
```

| Parametro | Default | Significato |
|---|---|---|
| `Threshold` | `1.5` | Sensibilità (più alto = meno anomalie) |
| `Seasonality` | `-1` | `-1` = auto-detect; `0` = nessuna; intero = numero di bin per ciclo |
| `Trend` | `'avg'` | `'avg'` (solo media), `'linefit'` (regressione lineare), `'none'` |
| `Test_points` | `0` | Punti finali da escludere dal training |
| `AD_method` | `'ctukey'` | `'ctukey'` (Tukey clippato) o `'tukey'` |
| `Seasonality_threshold` | `0.6` | Soglia score per auto-seasonality |

Restituisce **tre serie** allineate alla serie di input:

- `ad_flag` — ternaria: `+1` (spike), `-1` (dip), `0` (normale)
- `ad_score` — score continuo dell'anomalia (più alto = più anomalo)
- `baseline` — la curva attesa (utile per visualizzare lo "scostamento")

### 3.3 Esempio: anomaly detection su tutte le macchine

```kusto
let lookback   = 7d;
let bin_size   = 1m;
let threshold  = 2.5;
let last_only  = 5m;     // periodo di interesse "live"
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

Il pattern `make-series … | series_decompose_anomalies | mv-expand` è il caso d'uso classico. Funziona benissimo per **anomalie univariate**, una metrica per volta, eventualmente combinando con OR/AND.

### 3.4 Quando usarla

| Scenario | Adatto? |
|---|---|
| Una/poche metriche per macchina, anomalie su valore | ✅ |
| Pattern stagionali (turni, settimana, ciclo macchina) | ✅ con `linefit` + auto-seasonality |
| Centinaia/migliaia di serie | ✅ è vettorializzata |
| Anomalia "multivariata" (correlazione tra sensori) | ❌ una alla volta, non vede correlazioni |
| Pattern complessi (forme di segnale anomale) | ❌ serve modello custom |

### 3.5 Wrappare in update policy

Per attivare il near-real-time, si trasforma la query in **funzione** + **update policy**:

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
    | where ad_flag != 0 and ts > ago(bin_size * 2)   // solo l'ultimo bin
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

Da questo momento ogni nuovo batch in `measures_raw` triggera la funzione, e le righe anomale finiscono in `anomalies`.

> ⚠️ Una update policy con `make-series` su finestra di lookback è un po' più costosa di una sul singolo record perché ad ogni trigger ricostruisce la serie. Per alti throughput (>10k eventi/sec) conviene il pattern Python con stato precaricato.

---

## 4. Strada B — Modello custom in Python via plugin `python()`

È la strada per modelli che non si esprimono come decomposizione stagionale: Isolation Forest, One-Class SVM, autoencoder, gradient boosting, ecc.

### 4.1 Come funziona il plugin Python

```kusto
T 
| evaluate python(
    typeof(*, score:real, is_anomaly:bool),   // schema di output
    'python_code_string',                      // codice
    bag_pack('param1', value1, ...),           // dizionario kargs
    external_artifacts                         // opzionale, file da blob
)
```

**Variabili reserved:**

- `df` — pandas DataFrame con i dati di input (le righe che arrivano dal pipe)
- `kargs` — dizionario con i parametri passati con `bag_pack`
- `result` — pandas DataFrame con l'output (deve matchare lo schema dichiarato)

**Limiti del sandbox da tenere a mente:**

- Memoria limitata (ordine di GB, dipende dalla SKU dell'Eventhouse)
- Niente accesso di rete arbitrario
- I pacchetti devono essere in immagine, oppure caricati come `external_artifacts` zip
- Il timeout è breve (decine di secondi)

### 4.2 Pattern: modello salvato dentro Eventhouse

Lo schema standard per scoring in real-time:

1. **Training offline** (notebook Spark Fabric): legge lo storico, addestra, **serializza il modello in pickle**, lo encoda in base64 e scrive una riga nella tabella `models`.
2. **Funzione di scoring** in KQL legge l'ultimo modello dalla tabella, lo passa al plugin Python tramite `kargs`, scora il batch.
3. **Update policy** sulla tabella raw invoca la funzione e scrive in `anomalies`.

#### 4.2.1 Notebook di training (Fabric Spark)

```python
import mlflow
import pickle, base64, json
from datetime import datetime
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

# 1) Leggi storico via OneLake (Eventhouse → Delta su OneLake è on-demand)
abfss_uri = "abfss://<workspace>@onelake.dfs.fabric.microsoft.com/<eventhouse>.KQL/Tables/measures_raw"
df = spark.read.format("delta").load(abfss_uri).toPandas()

# 2) Filtra periodo "normal" e feature
features = ["temp", "vib", "press", "rpm"]
df_train = df[df["ts"] < "2026-04-01"]  # periodo di nominale
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

# 4) Logga su MLflow per lifecycle
with mlflow.start_run() as run:
    mlflow.sklearn.log_model(pipe, "iforest")
    mlflow.log_params({"n_estimators": 200, "contamination": 0.01})
    mlflow.log_metric("n_train_samples", len(X))
    run_id = run.info.run_id

# 5) Serializza per Eventhouse
model_bytes = pickle.dumps(pipe)
model_b64 = base64.b64encode(model_bytes).decode("ascii")
print(f"Model size: {len(model_bytes)/1024:.1f} KB")  # tieni occhio < 5-10 MB

# 6) Push in tabella `models` via Kqlmagic / Kusto SDK
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

# Ingestion in tabella models
ingest_client = QueuedIngestClient(kcsb)
props = IngestionProperties(database=db, table="models", data_format=DataFormat.CSV)
ingest_client.ingest_from_dataframe(row, ingestion_properties=props)
```

#### 4.2.2 Funzione KQL di scoring

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
# decision_function: più alto = più normale (sklearn convention)
scores = model.decision_function(X)
preds  = model.predict(X)   # -1 anomalo, +1 normale

result = df.copy()
result["score"] = -scores                  # invertito: più alto = più anomalo
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

Tutti i nuovi record che entrano in `measures_raw` vengono scorati; solo gli anomali finiscono in `anomalies`. La latenza tipica è di pochi secondi dal momento dell'ingestion.

### 4.3 Pattern: feature engineering temporale

Spesso l'anomalia non è sul singolo punto ma sulla forma del segnale (rolling stats, delta, derivata, FFT). Due strade:

**Opzione A — feature engineering dentro KQL prima del plugin**

```kusto
.create-or-alter function ScoreWithFeaturesV1(window_minutes:int = 5) {
    measures_raw
    | where ts > ago(2 * window_minutes * 1m)
    | partition hint.strategy=native by machine_id (
        order by ts asc
        | extend 
            temp_roll_mean  = row_window_session(temp,  window_minutes * 1m, 1m, ts != prev(ts)),
            temp_roll_std   = todouble(0),  // calcolato dentro Python
            temp_delta      = temp - prev(temp, 1)
    )
    | invoke ScoreWithIForest()
}
```

**Opzione B — feature engineering dentro Python**

Più flessibile ma il sandbox vede solo le righe del batch corrente. Per finestre lunghe servono **dati di lookback nel batch**, quindi la query passa più contesto del minimo.

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

# Mantieni solo i record "freschi" (gli altri erano lookback)
result = df[df["ts"] >= pd.Timestamp.utcnow() - pd.Timedelta(minutes=2)].copy()
    ```,
    bag_pack('model_b64', toscalar(models | top 1 by created_at desc | project model))
)
```

### 4.4 Pattern: modello pesante via `external_artifacts`

Se il pickle supera qualche MB (autoencoder, modelli ONNX), conviene **non** salvarlo in tabella ma in un blob/Lakehouse e referenziarlo come artefatto esterno:

```kusto
samples
| evaluate python(
    typeof(*, score:real, is_anomaly:bool),
    ```
import pickle
with open(r"C:\Temp\autoencoder.pkl", "rb") as f:
    model = pickle.load(f)
# ... resto dello scoring
    ```,
    bag_pack(),
    external_artifacts = dynamic({
        "autoencoder.pkl": "https://<lakehouse>/<path>/autoencoder.pkl?<sas>"
    })
)
```

Il file viene scaricato nel sandbox e disponibile come `C:\Temp\<nome>`.

### 4.5 Pattern: ONNX (consigliato per modelli pesanti / cross-framework)

ONNX è particolarmente adatto perché:

- Inferenza più veloce di sklearn nativo
- Indipendente dal framework di training (PyTorch, TF, sklearn)
- File compatto, deserializzazione rapida

```python
# In notebook training: converti sklearn → ONNX
from skl2onnx import to_onnx
onx = to_onnx(pipe, X[:1].astype(np.float32))
with open("/lakehouse/default/Files/iforest.onnx", "wb") as f:
    f.write(onx.SerializeToString())
```

Poi in KQL si usa `external_artifacts` puntando al file ONNX e si carica con `onnxruntime` nel sandbox.

---

## 5. Strada C — Multivariata "managed" (preview): `series_mv_*` + `time-series-anomaly-detector`

Microsoft ha rilasciato una capability multivariata managed che usa internamente un modello di Microsoft Research. Va abilitato il plugin **Python 3.11.7 DL** che include il pacchetto `time-series-anomaly-detector`.

L'approccio:

1. Training in notebook Spark con il pacchetto, modello salvato su MLflow
2. Path del modello (ABFSS) usato in KQL come `external_artifacts`
3. Funzione di scoring richiama il modello sul batch in arrivo

È la strada più "no-code-side" se il caso d'uso è classico (correlazione tra sensori, deriva di processo). Vincolo: occupa più risorse e richiede l'immagine DL (scelta di lock-in sulla SKU).

---

## 6. Update policy — meccanismi e best practice

### 6.1 Anatomia

```json
{
  "IsEnabled": true,
  "Source": "measures_raw",                  // tabella di input
  "Query": "ScoreWithIForest(measures_raw)", // funzione che produce le righe da scrivere
  "IsTransactional": false,                  // se true, fallimento = rollback ingestion
  "PropagateIngestionProperties": false      // propaga tag/metadata dall'ingestion
}
```

### 6.2 Punti di attenzione

- **`IsTransactional`** — su `false` per scoring ML: se il modello fallisce, la riga raw va comunque persistita. Su `true` solo se preferisci perdere il dato grezzo che non poter scorare.
- **Funzioni con stato (lookup tabella `models`)** — la query viene rieseguita ad ogni batch, quindi il `toscalar(models | ...)` viene rivalutato ogni volta. È un'OK perché tieni una sola riga "ultima" e il costo è trascurabile.
- **Update policy a cascata** — puoi avere policy che leggono da `anomalies` e scrivono in `anomalies_aggregated`, con regole tipo "se anomalia ripetuta in finestra X allora alarm di livello superiore".
- **Idempotenza** — se reingerisci lo stesso file, l'update policy gira di nuovo. Per evitare doppioni in `anomalies`, includi una chiave naturale + dedup periodico, oppure abilita `materialized-view` con `arg_max` come pattern di consolidamento.

### 6.3 Verifica e debug

```kusto
// Stato delle update policy
.show table anomalies policy update

// Failure delle update policy nelle ultime ore
.show ingestion failures
| where Table == "anomalies" and FailedOn > ago(1h)

// Audit di cosa la funzione produrrebbe SENZA scriverla
ScoreWithIForest(measures_raw | where ts > ago(5m)) | take 100
```

---

## 7. Activator (Reflex) — chiudere il loop sulle notifiche

Una volta che `anomalies` si popola, l'**Activator** è il pezzo che fa partire l'alert.

### 7.1 Due modalità

**A. Eventstream → Activator (sub-secondo)**

Si aggiunge l'Activator come destinazione dell'Eventstream stesso, oppure si aggancia l'Activator a un Eventstream che legge da `anomalies` (via OneLake availability + KQL DB source). Latenza: sotto al secondo. Adatto per alert "qualunque anomalia → notifica".

**B. KQL Queryset trigger su `anomalies` (poll, ~minuti)**

L'Activator esegue periodicamente (es. ogni 1-5 min) una query KQL che restituisce le anomalie da notificare. Permette logica complessa: "almeno N anomalie nella stessa macchina in 10 minuti", "anomalia su temp seguita da anomalia su vib entro 2 min", ecc. Latenza: pari al periodo di poll.

### 7.2 Esempio query di trigger con anti-flood

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

Sull'output di questa query l'Activator può configurare:

- Condizione: `n_anomalies >= 3`
- Action: email / Teams / Power Automate / webhook custom / esecuzione di una pipeline o notebook Fabric

### 7.3 Caveat
- L'Activator deve fare polling più frequente della finestra della query, altrimenti rischi di perdere alert.
- Non è "exactly-once": se due cicli si sovrappongono nella finestra, può duplicare. Per anomalie è in genere accettabile (meglio doppio che mancato), ma se serve dedup, usa una tabella di stato a parte.

---

## 8. Operazioni — retraining, drift, versionamento

### 8.1 Retraining schedulato

Una **Pipeline Fabric** che ogni N giorni:

1. Lancia il notebook di training
2. Il notebook scrive una nuova riga in `models` con `version` nuovo
3. La funzione di scoring prende automaticamente l'ultima (`top 1 by created_at desc`)

Niente da modificare in KQL: il deploy è "il modello è nella tabella".

### 8.2 Versionamento sicuro (canary)

Tieni una colonna `is_active` nella tabella `models` e una funzione che legge solo modelli `is_active == true`. Per validare un nuovo modello:

1. Inserisci v2 con `is_active = false`
2. Crea funzione di scoring `ScoreCanary` che usa esplicitamente `version == "v2"`, scrivi in `anomalies_canary`
3. Confronta v1 vs v2 su dati live per qualche giorno
4. Quando OK, swap dell'`is_active`

### 8.3 Drift monitoring

Un altro update policy / job schedulato che confronta la distribuzione delle feature recenti con quella di training:

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

Output → tabella `drift_alerts` → un altro Activator. Senza questo, in produzione industriale dopo qualche mese di solito iniziano falsi positivi/negativi senza che te ne accorga.

### 8.4 Eventhouse monitoring

Abilitare il **Workspace Monitoring** di Fabric ti dà tabelle `EventhouseCommandLogs`, `EventhouseDataOperations`, `EventhouseIngestionResultLogs` dove puoi vedere:

- Latenza di esecuzione delle update policy
- Failure dello scoring (es. modello incompatibile, OOM nel sandbox)
- Volume di ingestion vs scoring

Da queste si possono costruire altri Activator per "il modello sta fallendo".

---

## 9. Riepilogo decisionale — quale strada per quale caso

| Caso | Strada consigliata |
|---|---|
| Una metrica per macchina, pattern stagionale chiaro | **A** — `series_decompose_anomalies` |
| Tante metriche indipendenti, anomalie su valore | **A** ripetuta per ogni metrica |
| Correlazione tra sensori, modello custom | **B** — Isolation Forest in pickle + update policy |
| Pattern di forma (forme di onda, vibrazioni) | **B** — autoencoder via ONNX/external_artifacts |
| Multivariato classico managed | **C** — `time-series-anomaly-detector` |
| Modello molto pesante (DL grossa) | Endpoint esterno (Azure ML) — non trattato qui |

Il consiglio operativo: **partire da A** per avere una baseline in produzione in pochi giorni, poi affiancare **B** per i casi che A non copre. Le due strade convivono benissimo: stessa tabella `anomalies`, colonna `model_version` che distingue chi ha generato cosa.

---

## 10. Checklist di implementazione

- [ ] Eventhouse creato, OneLake availability ON
- [ ] Tabelle `measures_raw`, `anomalies`, `models` create con schema definitivo
- [ ] Eventstream agganciato a `measures_raw` (queued ingestion)
- [ ] Plugin Python abilitato (3.11.7 o DL se serve)
- [ ] Funzione native KQL `DetectAnomaliesNative` creata e testata su storico
- [ ] Notebook di training che scrive in `models` (e MLflow)
- [ ] Funzione `ScoreWithIForest` testata in modalità "ad-hoc" su batch storici
- [ ] Update policy attivata su `anomalies` con `IsTransactional=false`
- [ ] Activator agganciato ad `anomalies` con anti-flood
- [ ] Pipeline schedulata di retraining (settimanale/mensile)
- [ ] Drift monitoring + Activator di livello 2
- [ ] Workspace monitoring abilitato per visibilità operativa
