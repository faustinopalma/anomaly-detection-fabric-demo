# Training dei modelli CNC M-002 / M-003 — resoconto e procedura di ricostruzione

_Ultimo aggiornamento: 2026-06-05_

Questo documento racconta **com'è andato** il training degli autoencoder di
anomaly detection per i due mandrini CNC a 3 sensori (`M-002`, `M-003`) e
spiega **come ricrearlo** da zero. Lo stesso codice gira in modo identico in
locale e su Azure ML — l'unica differenza è il wrapper che lo lancia.

- Logica di training (unica fonte di verità): [`tools/cnc_ae_lab.py`](../tools/cnc_ae_lab.py)
- Driver Azure ML (submit + stream + download): [`cloud-training/submit_cnc_sota.py`](../cloud-training/submit_cnc_sota.py)
- Entry-point sul nodo AML: [`cloud-training/src_cnc_sota/entry_cnc_sota.py`](../cloud-training/src_cnc_sota/entry_cnc_sota.py)
- Registrazione live in Fabric: [`tools/05_register_model.py`](../tools/05_register_model.py)

---

## 1. Cosa è stato addestrato (risultati)

Entrambi i modelli sono **autoencoder Transformer non supervisionati**
(ricostruzione su dati normali; nessuna etichetta in training). Precision e
recall sono misurati **dopo**, su un set di eval con anomalie sintetiche
iniettate (spike / drift / stuck) tenute fuori dal training.

| Macchina | Variante vincente | Parametri | Soglia | Precision | Recall | F1 | PR-AUC |
|---|---|---:|---:|---:|---:|---:|---:|
| **M-003** | `base56` (ricostruzione) | 161.419 | 1.9374 | 0.954 | 0.869 | 0.910 | 0.953 |
| **M-002** | `denoise64d` (denoising, encoder a 3 layer) | 234.275 | 2.3964 | 0.943 | 0.730 | 0.823 | 0.933 |

Ultima esecuzione su Azure ML: job `mango_muscle_vr3zgcqqzq` (cpu-cluster),
~150–175 s per variante a 18 epoche. Modelli live: **M-003 v2**, **M-002 v4**.

### Perché M-002 ha recall più basso

I dati normali di M-002 (traccia synthgen) hanno una **coda pesante**: il punto
di best-F1 (0.847 @ soglia 2.16) costerebbe un **FPR del 5,3%** per finestra —
troppi falsi allarmi in produzione. La soglia viene quindi **vincolata al
p97** dei punteggi normali (≤3% FPR), che cade sul ginocchio della curva
precision/recall (vedi §4). M-003, avendo normali più puliti, non è vincolato:
la sua soglia coincide con il best-F1.

---

## 2. Architettura e contratto della pipeline

Costanti fisse in [`tools/cnc_ae_lab.py`](../tools/cnc_ae_lab.py) (devono
combaciare con lo scorer KQL):

- `WINDOW = 64`, `STRIDE_TRAIN = 16`, `STRIDE_EVAL = 1`, `SEED = 1337`
- Vincolo dimensione: FP16 ONNX base64 ≤ 1 MB (budget riga Kusto) →
  `MAX_PARAMS = 245_000`
- Score = `maxmean` (max-sui-sensori della media-nel-tempo dell'errore di
  ricostruzione)
- Loss composita MSE + 0,1·L1(delta), `norm_first=True`, GELU

Lo sweep prova 6 varianti (ricostruzione vs denoising, due capacità, due
aggregazioni), seleziona per F1 (tie-break PR-AUC), poi **ri-addestra il
vincitore su tutti i dati normali** e calibra la soglia su una eval iniettata
fresca/held-out.

> ⚠️ La **soglia non è incorporata nell'ONNX**. Lo scorer KQL legge
> `metadata.threshold` a runtime, quindi ritararla = modificare `metadata.json`
> + ri-registrare (nessun re-training necessario).

---

## 3. Sorgenti dati (coerenza train/serve)

I dati vengono dallo **stesso codice del simulatore**, così training e
inferenza usano la stessa distribuzione:

- **M-003** → profilo CNC reale:
  `cnc_engine.generate_frame(data/cnc_profile_M-003.json)` (~181k campioni
  attivi a 120 h).
- **M-002** → traccia synthgen completa:
  `_local/synthgen/synth_trace_full.npz` (~86k campioni attivi). Poiché è una
  traccia **finita**, si riserva una coda held-out (82%/18%) per la soglia e le
  metriche finali honest. Fallback: `simulator-cloud/src/synth_trace_M-002.json`.

Le anomalie sintetiche per la eval (spike/drift/stuck, scale-aware) replicano
verbatim le costanti di `simulator-cloud/src/simulate_machines.py`
(`SPIKE_SIGMA_K`, `DRIFT_SIGMA_K`, durate per livello), così l'eval rispecchia
ciò che il simulatore live emette davvero.

---

## 4. Regola della soglia (storia ed evoluzione)

Soglia di deploy = `max(best_F1_thr, normal_p97)`:

| Versione | Floor | Effetto su M-002 |
|---|---|---|
| v1 | p99.5 (~0,5% FPR) | recall schiacciato (floor 3,168 > best_thr 2,160) |
| v2 | p98 (~2% FPR) | ancora vincolato → soglia 2,591, recall 0,683, F1 0,799 |
| **v3 (attuale)** | **p97 (~3% FPR)** | soglia 2,396, recall 0,730, **F1 0,823** |

Curva precision/recall/FPR di M-002 (dal modello finale) attorno al ginocchio:

| soglia | precision | recall | F1 | FPR |
|---:|---:|---:|---:|---:|
| 2,16 (best-F1) | 0,910 | 0,791 | 0,847 | 5,29% |
| **2,40 (p97)** | **0,943** | **0,730** | **0,823** | **3,01%** |
| 2,59 (p98) | 0,959 | 0,683 | 0,798 | 1,98% |

M-003 non è toccato dal floor (best_thr 1,937 > p97).

---

## 5. Ricreare il training IN LOCALE

Prerequisiti: il virtualenv del repo (`.venv`) con `torch`, `onnx`,
`onnxruntime`, `numpy`. La sorgente dati di M-002
(`_local/synthgen/synth_trace_full.npz`) deve essere presente.

```powershell
# Attiva il venv del repo
.\.venv\Scripts\Activate.ps1

# Sweep + retrain + export per M-003 (scrive in models/transformer_ae_small__M-003/)
.venv\Scripts\python.exe tools\cnc_ae_lab.py M-003 --epochs 18 --save

# Sweep + retrain + export per M-002 (scrive in models/transformer_ae_small__M-002/)
.venv\Scripts\python.exe tools\cnc_ae_lab.py M-002 --epochs 18 --save

# Smoke test rapido (sweep ridotto a 2 varianti, niente export):
.venv\Scripts\python.exe tools\cnc_ae_lab.py M-003 --quick
```

Flag principali:

- `--epochs N` — epoche per variante (default 18).
- `--hours H` — ore di telemetria sintetica per M-003 (default 120; ignorato da M-002).
- `--quick` — sweep minimo a 2 varianti per smoke test.
- `--save` — esporta gli artefatti in `models/` (senza, è un dry-run).

Output per ciascun modello in `models/transformer_ae_small__M-00X/`:
`model.onnx` (FP32), `model.fp16.onnx` (FP16, quello deployato), `model.pt`,
`scaler.json`, `metadata.json`. Su CPU il tempo è ~150–175 s per variante
(circa 15–20 min totali a macchina con 6 varianti + retrain).

---

## 6. Ricreare il training SU AZURE ML

Stesso codice (`cnc_ae_lab.py`), wrappato. Il driver mette in staging i file
minimi, sottomette un command job, fa streaming dei log e scarica gli artefatti
in `models/`.

```powershell
# Default: M-003 + M-002, 18 epoche, 120 h, cpu-cluster
.venv\Scripts\python.exe cloud-training\submit_cnc_sota.py

# Solo una macchina
.venv\Scripts\python.exe cloud-training\submit_cnc_sota.py --machines M-003

# Su GPU (più veloce; ma la capacità T4 in westeurope è inaffidabile)
.venv\Scripts\python.exe cloud-training\submit_cnc_sota.py --compute gpu-t4-cluster
```

Workspace: `anomalyml-mlw` (rg `rg-anomaly-ml-westeurope`). Compute affidabile:
`cpu-cluster` (Standard_DS3_v2, scale-to-zero). `gpu-t4-cluster` accelera (~18 s
vs ~160 s per variante) ma in westeurope va spesso in coda per mancanza di
capacità → fallback su CPU.

### Note infrastrutturali (gotcha)

- L'output del command job AML sta nel datastore **`workspaceartifactstore`**
  (container `azureml`, path `ExperimentRun/dcid.{job}/outputs/`), **non** in
  `workspaceblobstore`. `submit_job.download_models()` lo gestisce.
- Lo storage `stanomalymlfknlnf4v` ha `publicNetworkAccess=Disabled`. Per
  l'upload del codice da locale va riaperto temporaneamente e **ripristinato a
  `Disabled`** a fine task:
  ```powershell
  az storage account update --name stanomalymlfknlnf4v `
    -g rg-anomaly-ml-westeurope --public-network-access Disabled
  ```
- Il download richiede ruolo **Storage Blob Data Reader** (auth via AAD, la
  shared key è disattivata).

---

## 7. Deploy (registrazione live in Fabric)

Dopo aver prodotto/scaricato gli artefatti in `models/`:

```powershell
# Una macchina alla volta (distanzia le chiamate: 429 KustoThrottling)
.venv\Scripts\python.exe tools\05_register_model.py models/transformer_ae_small__M-003
.venv\Scripts\python.exe tools\05_register_model.py models/transformer_ae_small__M-002
```

Ogni registrazione incrementa la versione nella tabella `models`. Lo scorer KQL
`fn_score_demo_M002` / `fn_score_demo_M003` è generico (legge
window/sensors/scaler/threshold dalla riga più recente), quindi **non serve
modificare KQL o il simulatore**. Il comando `.set-or-append` inline da
~650–880 KB può scatenare **429 KustoThrottling** sull'Eventhouse: distanziare e
riprovare.

> ⚠️ M-001 e M-004 sono modelli verificati e **non vanno toccati** da questa
> procedura (sono macchine fisiche a 8 sensori, pipeline diversa).

---

## 8. File di riferimento

| File | Ruolo |
|---|---|
| [`tools/cnc_ae_lab.py`](../tools/cnc_ae_lab.py) | Logica di training, sweep, eval, export, regola soglia |
| [`tools/train_cnc_m003.py`](../tools/train_cnc_m003.py) | Export ONNX Kusto-safe (riusato dal lab) |
| [`cloud-training/submit_cnc_sota.py`](../cloud-training/submit_cnc_sota.py) | Driver Azure ML |
| [`cloud-training/src_cnc_sota/entry_cnc_sota.py`](../cloud-training/src_cnc_sota/entry_cnc_sota.py) | Entry-point sul nodo AML |
| [`tools/05_register_model.py`](../tools/05_register_model.py) | Registrazione modello live in Fabric |
| [`docs/cloud_vs_local_training_comparison.md`](cloud_vs_local_training_comparison.md) | Confronto tempi cloud vs locale |
| `models/transformer_ae_small__M-002/` · `__M-003/` | Artefatti prodotti (ONNX/scaler/metadata) |
