# Real-Time Anomaly Detection on Microsoft Fabric

Real-time anomaly detection for a fleet of factory machines. Each machine
streams multi-sensor telemetry; a windowed model, trained offline and exported
to **ONNX**, runs **inside the Fabric KQL database** (via the `python()`
plugin) and promotes anomalous windows into an `anomalies` table that drives
dashboards and alerts — with no external Spark or AKS cluster.

This page gives a structured, end-to-end view of the production solution.
[Appendix A](#appendix-a--explorations-not-selected-for-production) records the
alternatives that were evaluated and not adopted, with the rationale.

---

## 1. Problem and approach

Industrial machines emit a continuous stream of measurements, and the goal is
to raise an alert **within seconds** when those measurements look "wrong"
relative to normal behaviour. The problem decomposes into three layers:
**ingest** the data reliably, **decide** what is anomalous, and **react**.

The detection approach is **unsupervised, reconstruction-based**. An
autoencoder is trained **on normal data only**; at inference, the
**reconstruction error** of a window is the anomaly score, and a **threshold**
turns that score into `is_anomaly ∈ {0, 1}`. No fault labels are needed in
production: precision and recall are measured **after the fact** by injecting
synthetic anomalies (see §3.3).

For the conceptual background in plain English, see [`concepts.md`](concepts.md).

---

## 2. Production pipeline

```
┌──────────────┐   ┌─────────────┐   ┌────────────────────────┐   ┌───────────┐   ┌──────────┐
│ Always-on    │──>│ Eventstream │──>│      Eventhouse        │──>│  Reflex   │──>│  Alerts  │
│ simulator    │   │ es_machines │   │     raw_telemetry      │   │(Activator)│   │ Teams /  │
│ (4 machines) │   │ (managed    │   │          │             │   └───────────┘   │  email   │
└──────────────┘   │  pipe)      │   │ per-machine update     │        └──────────┘
                   └─────────────┘   │ policy fn_score_demo   │   ┌──────────────────────┐
                                     │          ▼             │──>│ Real-Time Dashboard  │
                                     │      anomalies         │   │  rtd_telemetry_live  │
                                     └────────────────────────┘   └──────────────────────┘
```

Two tables (`raw_telemetry` → `anomalies`), one mechanism (**update policy**)
that promotes anomalous rows, and one watcher (**Reflex**) that reacts. The
deployed items and their wiring are detailed in
[`architecture.md`](architecture.md).

---

## 3. The selected stack

| Concern | Production choice | Deep dive |
|---|---|---|
| Ingestion | Always-on simulator → Eventstream `es_machines` → Eventhouse | [§3.1](#31-ingestion) · [architecture.md](architecture.md) |
| Model | `transformer_ae_small` **per machine** (Transformer autoencoder, WINDOW=64) | [§3.2](#32-the-model-per-machine-transformerae-small) · [model_architecture_options.md](model_architecture_options.md) |
| Training & threshold | `tools/cnc_ae_lab.py` — variant sweep, injected eval, p97 threshold floor | [§3.3](#33-training-and-threshold-calibration) · [cnc_sota_training.md](cnc_sota_training.md) |
| Deployment | FP16 ONNX **inline** in a `models` table row, scored in-KQL via `python()` | [§3.4](#34-deployment-inline-onnx-in-kql) · [model_deployment_options.md](model_deployment_options.md) |
| Live fleet | 4 machines ingested, **3 scored** by dedicated models | [§3.5](#35-the-live-fleet) · [architecture.md §2b](architecture.md) |
| Data modeling | `raw_telemetry` long + wide materialized view for multivariate scoring | [§3.7](#37-data-modeling) · [data_modeling_industrial_measures.md](data_modeling_industrial_measures.md) |
| Reaction | Real-Time Dashboard + Reflex; Entra-gated control panel | [§3.6](#36-simulator-and-control-panel) · [../simulator-cloud/README.md](../simulator-cloud/README.md) |

### 3.1 Ingestion

An **always-on simulator** (Azure Container App `ca-simulator`) emits telemetry
24/7 with no gaps and pushes it into **Eventstream** `es_machines`, which
delivers it to the `raw_telemetry` table in the **Eventhouse** (`eh_telemetry` /
`kql_telemetry`). The same stream also feeds a Lakehouse for cold storage. See
[`architecture.md`](architecture.md) §2 and §4.

### 3.2 The model: per-machine TransformerAE-small

The solution uses a **small Transformer autoencoder** (`transformer_ae_small`,
WINDOW=64) trained **once per machine** — each with its own model, scaler, and
threshold (read from `metadata.threshold`). There is no hard-coded `machine=`
filter in the scoring path: the KQL scorer is **generic** and reads everything
from the model's metadata.

Key constraint: the model must fit in a **single Kusto row (~1 MB base64)**, so
it is exported as a **single-file FP16 ONNX** (`MAX_PARAMS ≈ 245k`). The
comparison with other model families (LSTM AE, Conv+GRU, large Transformer) is
in [model_architecture_options.md](model_architecture_options.md); the rejected
alternatives are summarized in
[Appendix A](#appendix-a--explorations-not-selected-for-production).

### 3.3 Training and threshold calibration

Training logic lives in **[`tools/cnc_ae_lab.py`](../tools/cnc_ae_lab.py)**, the
single source of truth (it runs identically locally and on Azure ML). The
procedure:

1. sweep 6 variants (reconstruction vs denoising, two capacities, two
   aggregations), selecting by F1 (PR-AUC as tie-break);
2. retrain the winner on **all** normal data;
3. held-out **injected eval**: synthetic anomalies (spike/drift/stuck,
   scale-aware) that mirror the live simulator's overlay constants, with
   window-level ground truth;
4. deployment threshold = `max(best_F1_thr, normal_p97)` (≤3% per-window FPR);
5. export a Kusto-safe FP16 ONNX (parity <0.01% vs FP32 → live == offline).

The threshold is **not baked into the ONNX**: the KQL scorer reads
`metadata.threshold` at runtime, so re-tuning means editing `metadata.json` and
re-registering — no retraining required.

The full training report for M-002/M-003 (results, data sources, and commands
to recreate the training locally and on Azure ML) is in
[cnc_sota_training.md](cnc_sota_training.md).

### 3.4 Deployment: inline ONNX in-KQL

Among the deployment patterns evaluated
([model_deployment_options.md](model_deployment_options.md)), production uses
**Pattern C — inline ONNX**: each model lives as a row in the `models` table;
the scoring function loads it from the latest row and runs `python(onnx)`
inside the Eventhouse. This means no external infrastructure, no per-invocation
egress, and instant, reversible version switching. The external-artifact
patterns (blob URL) were rejected (see Appendix A).

### 3.5 The live fleet

The simulator **ingests 4 machines**; **3 are scored** by a dedicated model:

| Machine | Model (scoring) | Sensors | Data source |
|---|---|---|---|
| M-001 | `transformer_ae_small__M-001` | 8 (synthetic) | simulator physics |
| M-002 | `transformer_ae_small__M-002` | 3 (spindle load/power/torque) | synthgen replay trace |
| M-003 | `transformer_ae_small__M-003` | 3 (spindle load/power/torque) | recorded real CNC profile |
| M-004 | — (ingested, **not scored**) | 8 (synthetic) | simulator physics |

M-004 is a trained benchmark model that is **not registered** in the `models`
table (telemetry without detections). Details in
[architecture.md §2b](architecture.md).

### 3.6 Simulator and control panel

The same Container App serves an **operator control panel** (same-origin
FastAPI + static `webapp/`) protected by **Microsoft Entra ID**: live machine
state, forced state, anomaly toggle/injection, and a client-side 5-minute live
chart. See [`../simulator-cloud/README.md`](../simulator-cloud/README.md) and
[`../webapp/README.md`](../webapp/README.md).

### 3.7 Data modeling

`raw_telemetry` is stored in **long** format (one row per measurement). For
multivariate scoring, a **wide materialized view** (`raw_telemetry_wide_mv`)
presents one row per `(machine_id, ts_bin=1s)` with one column per sensor,
maintained automatically by the Eventhouse. The alternatives (pure long vs wide
vs hybrid) are covered in
[data_modeling_industrial_measures.md](data_modeling_industrial_measures.md).

---

## 4. Documentation map

Suggested reading order:

| Doc | Contents |
|---|---|
| [solution.md](solution.md) | The production solution end-to-end + appendix of non-production explorations |
| [concepts.md](concepts.md) | Plain-English tour of the architecture and design choices |
| [architecture.md](architecture.md) | Deployed items and how they are wired |
| [cnc_sota_training.md](cnc_sota_training.md) | M-002/M-003 training report + how to recreate it |
| [anomaly_detection_fabric_kql.md](anomaly_detection_fabric_kql.md) | KQL cookbook: every in-Eventhouse detection option, with code |
| [data_modeling_industrial_measures.md](data_modeling_industrial_measures.md) | Long vs wide vs hybrid table designs for heterogeneous measures |
| [model_architecture_options.md](model_architecture_options.md) | Model families (AE variants) and the tradeoffs behind the choice |
| [model_deployment_options.md](model_deployment_options.md) | Where/how to run the ONNX model and the deployment tradeoffs |
| [cloud_vs_local_training_comparison.md](cloud_vs_local_training_comparison.md) | Azure ML GPU vs local CPU training benchmark |
| [RUNBOOK.md](RUNBOOK.md) | Fresh-machine recipe: from `git clone` to a working environment |

---

## Appendix A — Explorations not selected for production

Alternatives evaluated during development and not adopted. They remain in the
repository as reference, baselines, or teaching material.

| Exploration | Where | Why it is not in production |
|---|---|---|
| **Univariate LSTM AE** (per sensor) | [notebooks/02_train_univariate_ae.ipynb](../notebooks/02_train_univariate_ae.ipynb) | A single hidden state compresses the whole window → loses fine temporal detail. Kept as a **sanity baseline**. |
| **Multivariate LSTM AE** (per machine) | [notebooks/03_train_multivariate_ae.ipynb](../notebooks/03_train_multivariate_ae.ipynb) | A global μ+Kσ threshold across different regimes (IDLE vs PRODUCTION_HEAVY) → false positives on legitimate transients. |
| **Conv1D + GRU AE** | [notebooks/05_train_conv_gru_ae.ipynb](../notebooks/05_train_conv_gru_ae.ipynb) · `models/conv_gru_ae/` | First "production-ready" choice (fit the Kusto row), later **superseded** by TransformerAE-small with higher PR-AUC. |
| **Large TransformerAE** | `models/transformer_ae/` | The exporter spilled weights into an external `.data` file → not a single-file ONNX, **does not fit the Kusto row** → not deployable inline. Resolved by shrinking the model (see §3.2). |
| **Generic univariate/multivariate KQL toolbox** (single function with a `machine=` filter) | [kql/03_scoring_functions.kql](../kql/03_scoring_functions.kql) | Superseded by the **per-machine** pattern (`fn_score_demo_M00X`): one model/threshold per machine, no hard-coded filter. |
| **External-artifact ONNX deployment** (Patterns A/B, blob URL in `external_artifacts`) | [model_deployment_options.md §2](model_deployment_options.md) | Kusto pre-fetches the blob on **every** sandbox invocation (~50–200 ms + egress) even when the inline model is active → rejected in favour of inline Pattern C. |
| **Hot/cold tiering and external compute** | [model_deployment_options.md §4](model_deployment_options.md) | Not needed at the current scope (a small model that fits inline). Documented as a future path if the model zoo grows. |
| **GPU T4 training (Azure ML)** | [cloud_vs_local_training_comparison.md](cloud_vs_local_training_comparison.md) · `cloud-training/` | Faster per variant, but **T4 capacity in westeurope is unreliable** (jobs queue >9 min). The reliable path is `cpu-cluster`. Benchmark retained. |
| **Synthetic generator (synthgen)** | `tools/build_synth_trace.py` · `_local/synthgen/` | A generative model used **offline** to produce the M-002 replay trace; it is **not** a runtime scoring component. |

M-001 and M-004 (8-sensor machines, simulator physics) use a different training
pipeline (`tools/train_per_machine.py`) and are **not** touched by the SOTA CNC
procedure for M-002/M-003.
