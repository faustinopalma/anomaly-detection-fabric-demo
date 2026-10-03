# anomaly-detection-fabric-demo

**[Read the illustrated guide (Italian) →](https://faustinopalma.github.io/anomaly-detection-fabric-demo/)**

## Executive summary

**The goal is to complement Microsoft Fabric's native anomaly detection with custom models for more complex industrial monitoring scenarios.** Some anomalies depend on the relationship between several sensors, how their signals evolve over time, and whether a machine is idle or producing. For example, spindle power and torque may each remain within their usual ranges while their combination is unusual for the workload.

This demo implements a machine-specific detection pipeline within Fabric, from incoming telemetry to a dashboard showing detected anomalies. Models learn normal behaviour from examples and flag deviations for investigation. Training runs locally or on Azure Machine Learning; detection runs inside Fabric, without a separate model-serving cluster. The intended benefit is to extend the scenarios covered while keeping data ingestion, detection, and monitoring on the same platform.

## How this complements native detection

Fabric's native KQL functions, such as [`series_decompose_anomalies()`](https://learn.microsoft.com/en-us/kusto/query/series-decompose-anomalies-function?view=microsoft-fabric), detect deviations in individual time series after accounting for trend and seasonality. They provide a starting point without a custom model to train or maintain. This repository explores a complementary approach when the application needs:

- **Relationships between sensors:** assess measurements such as load, power, and torque together, including unusual combinations of otherwise plausible values.
- **Patterns over time:** analyse a sequence of measurements to capture changes in behaviour that a single reading may not reveal.
- **Machine-specific operating context:** use a dedicated model and detection threshold for each machine, with an activity filter to exclude idle periods outside the model's training conditions.

The implemented demo focuses on the custom-model path. It includes controlled anomaly injection and checks for detections, missed events, and false alarms. These tests demonstrate the integration and its behaviour on the evaluated data; they do not establish superiority over native detection or validated diagnosis of real equipment faults. Short spikes and frozen sensor values can still be missed, and unfamiliar operating conditions can produce false alarms.

## Demo architecture

The detector is a **Transformer autoencoder**, a model trained to reconstruct normal sequences of sensor readings. A high reconstruction error indicates a possible anomaly. Each machine has its own model, input scaling, and threshold. Models are exported to **ONNX**, a portable model format, and executed in the Fabric KQL database through the `python()` plugin. Thresholds are read from `metadata.threshold` and can be adjusted without retraining.

| Machine | Model (scoring) | Sensors | Data source |
| --- | --- | --- | --- |
| M-001 | `transformer_ae_small__M-001` | 8 (synthetic) | simulator physics |
| M-002 | `transformer_ae_small__M-002` | 3 (CNC spindle: `mandrino_load`/`power`/`torque`) | synthgen replay trace |
| M-003 | `transformer_ae_small__M-003` | 3 (CNC spindle: `mandrino_load`/`power`/`torque`) | recorded real CNC profile |
| M-004 | — (ingested, not scored) | 8 (synthetic) | simulator physics |

Pipeline: **cloud simulator** (Azure Container Apps) → **Eventstream** → **Eventhouse** (`raw_telemetry` → per-machine model scoring → `anomalies`) → **Real-Time Dashboard**. Scoring runs on ingested batches through update policies, so detection is near real time rather than instantaneous.

The simulator also serves a **control panel protected by Microsoft Entra ID** to view live telemetry, change machine states, inject anomalies, and compare those injections with detections returned by Fabric. See [webapp/README.md](webapp/README.md).

The table describes the documented demo configuration. The M-004 model is a cloud-versus-local training benchmark; enabling its scoring requires model registration and an update-policy entry. Deployment details are in [docs/architecture.md](docs/architecture.md).

## Documentation

The illustrated guide linked above covers the pipeline, model, and evaluation limits, with diagrams and an interactive score example. Its [self-contained HTML source](site/index.html) also opens locally.

The [Pages workflow](.github/workflows/pages.yml) publishes only `site/`, excluding data, model binaries, local configuration, and session notes. Updates to the page on `main` trigger publication; the workflow can also be run manually. For a fork, first select **Settings > Pages > Build and deployment > Source: GitHub Actions** in that repository.

To show the guide in the repository sidebar, edit **About** (gear icon) and enable **Use your GitHub Pages website**. If that option is unavailable, paste the published URL into **Website**.

For implementation details, follow the guides below.

| Doc | What you get |
| --- | --- |
| [`docs/solution.md`](docs/solution.md) | End-to-end implementation and rationale, including alternatives evaluated. |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | **Fresh-machine recipe**: clone → working environment in ~12 sequential steps. Use this when bringing up a new PC or Remote Tunnel. |
| [`docs/concepts.md`](docs/concepts.md) | Plain-English tour of the architecture and the design choices behind it. |
| [`docs/architecture.md`](docs/architecture.md) | Deployed pieces of this demo (items, names, post-deploy steps). |
| [`docs/cnc_sota_training.md`](docs/cnc_sota_training.md) | How the live M-002/M-003 models were trained, the results, and how to recreate the training (local + Azure ML). |
| [`docs/anomaly_detection_fabric_kql.md`](docs/anomaly_detection_fabric_kql.md) | KQL cookbook: every available path for in-Eventhouse anomaly detection, with code. |
| [`docs/data_modeling_industrial_measures.md`](docs/data_modeling_industrial_measures.md) | How to shape tables when measurements come in heterogeneously (long vs wide vs hybrid). |
| [`docs/model_architecture_options.md`](docs/model_architecture_options.md) | Model-family options (AE variants) and the tradeoffs behind the chosen TransformerAE. |
| [`docs/model_deployment_options.md`](docs/model_deployment_options.md) | Where/how to run the ONNX model (in-KQL, Spark, local) and the deployment tradeoffs. |
| [`docs/cloud_vs_local_training_comparison.md`](docs/cloud_vs_local_training_comparison.md) | Azure ML GPU vs local CPU training benchmark (M-004). |
| [`simulator-cloud/README.md`](simulator-cloud/README.md) | Always-on simulator on Azure Container Apps (24/7, no-gap telemetry) + optional Entra-gated control API. |
| [`webapp/README.md`](webapp/README.md) | Entra ID–gated operator control panel: live state, force-state, anomaly inject, 5-minute live chart. |
| [`tools/README.md`](tools/README.md) | Local simulator + CLI helpers used to set up Eventstream, run KQL scripts, register models, build the dashboard. |

## Prerequisites

- Windows / macOS / Linux with [PowerShell 7+](https://learn.microsoft.com/powershell/scripting/install/installing-powershell)
- Python 3.10+ (for `pip install ms-fabric-cli`)
- An existing Fabric **capacity** you can assign workspaces to
- An Entra account with rights on that capacity
- Tenant admin must have enabled "Users can use Fabric APIs"
- For the in-KQL ONNX scoring: the `python()` plugin enabled on the
  Eventhouse (admin toggle)

## Setup

> For a complete fresh-machine recipe (clone → live ingestion in ~12
> sequential steps) see [`docs/RUNBOOK.md`](docs/RUNBOOK.md). The
> abridged version below covers only the happy path on a machine that
> already has Python, PowerShell and Azure CLI.

```powershell
# 1. Install the Fabric CLI (once)
pip install --upgrade ms-fabric-cli

# 2. Configure local secrets
Copy-Item .env.example .env
# edit .env and fill in tenant id, capacity name, workspace name, etc.

# 3. Run the bootstrap script
./scripts/deploy.ps1
```

The first run launches a **device-code login** in your browser. The token
is cached under `~/.config/fab/` (gitignored) so subsequent runs are
silent until it expires.

## Layout

```text
.
├── .env.example                          # template; copy to .env (gitignored)
├── README.md
├── docs/
│   ├── solution.md                              # production entry point — start here
│   ├── concepts.md                              # plain-English tour
│   ├── architecture.md                          # deployed items + post-deploy steps
│   ├── cnc_sota_training.md                     # M-002/M-003 training report + how to recreate
│   ├── anomaly_detection_fabric_kql.md          # KQL cookbook (every option, with code)
│   ├── data_modeling_industrial_measures.md     # long vs wide vs hybrid table designs
│   ├── model_architecture_options.md            # AE model-family options + tradeoffs
│   ├── model_deployment_options.md              # where/how to run the ONNX model
│   ├── cloud_vs_local_training_comparison.md    # Azure ML GPU vs local CPU benchmark
│   └── RUNBOOK.md                               # fresh-machine recipe
├── kql/
│   ├── 01_tables.kql                     # raw_telemetry, anomalies, batching policy, streaming OFF
│   ├── 02_models.kql                     # versioned ONNX model registry
│   ├── 03_scoring_functions.kql          # univariate + multivariate window builders, python(onnx) scorers
│   ├── 04_update_policy.kql              # per-machine auto-score on ingest (fn_score_demo_M001/M002/M003)
│   ├── 05_multivariate_mv.kql            # wide materialized view + multivariate scoring helpers
│   ├── 05_injections.kql                 # injected_anomalies ground-truth table
│   ├── 06_correlation.kql                # injection↔detection correlation functions
│   └── 07_classification.kql             # TP/FP/FN classification functions
├── items/                                # blank scaffold, kept for the only legacy notebook still in use
│   └── nb_register_kql_scorer.Notebook/      # re-applies kql/*.kql
├── notebooks/                            # active notebooks (publish via tools/upload_notebook.py)
│   ├── 01_simulator_dev.ipynb            # physics simulator + offline dataset builder (data/training, data/eval)
│   ├── 02_train_univariate_ae.ipynb      # per-sensor LSTM AE → univariate_ae__<sensor_id>
│   ├── 03_train_multivariate_ae.ipynb    # per-machine LSTM AE over wide MV → multivariate_ae__<machine_id>
│   ├── 04_train_transformer_ae.ipynb     # TransformerAE variant
│   ├── 05_train_conv_gru_ae.ipynb        # Conv+GRU AE variant
│   ├── 06_train_transformer_small.ipynb  # small TransformerAE (the live per-machine model)
│   ├── 07_explore_telemetry.ipynb        # ad-hoc telemetry exploration
│   ├── 08_simulator_cnc_dev.ipynb        # CNC (M-003) profile + engine development
│   └── 09_cloud_train_aml.ipynb          # submit Azure ML cloud training jobs
├── tools/                                # Python helpers (Eventstream wiring, KQL setup, model register, dashboard, anomaly inject, correlate)
├── simulator-local/                      # run the simulator locally
├── simulator-cloud/                      # always-on simulator on Azure Container Apps
├── cloud-training/                       # Azure ML job: generate synthetic data + train + export ONNX
├── infra/
│   ├── fabric-capacity.bicep             # Bicep template for a Microsoft.Fabric/capacities resource
│   └── ml-workspace.bicep                # Bicep template for the Azure ML training workspace
└── scripts/
    ├── create-capacity.ps1               # one-shot: create the Fabric capacity (uses infra/fabric-capacity.bicep)
    ├── deploy.ps1                        # main entrypoint: workspace + items on an existing capacity
    └── lib/
        ├── env.ps1                      # .env loader + validation
        └── fabric.ps1                   # thin idempotent helpers around `fab`
```

## What the script creates

All items below are **blank container items**. The `nb_register_kql_scorer`
notebook ships with a starter scaffold from `items/`. The active training
notebooks (`01_simulator_dev`, `02_train_univariate_ae`,
`03_train_multivariate_ae`) live under `notebooks/` and are published as
Fabric Notebook items separately with
[`tools/upload_notebook.py`](tools/upload_notebook.py); see
[`docs/architecture.md`](docs/architecture.md) §3 and §4.6.

| Item             | Name (default)            | Type           |
|------------------|---------------------------|----------------|
| Workspace        | `anomaly-detection-dev`   | Workspace      |
| Eventstream      | `es_machines`             | Eventstream    |
| Eventhouse       | `eh_telemetry`            | Eventhouse     |
| KQL Database     | `kql_telemetry`           | KQLDatabase    |
| Lakehouse        | `lh_telemetry`            | Lakehouse      |
| Environment      | `env_anomaly`             | Environment    |
| Notebook         | `nb_register_kql_scorer`  | Notebook       |
| Data Pipeline    | `pl_retrain`              | DataPipeline   |
| Reflex           | `act_anomaly_alerts`      | Reflex         |
| Semantic Model   | `sm_anomaly`              | SemanticModel  |
| Report           | `rpt_anomaly`             | Report         |

In addition, after running the training notebooks once, two more Notebook
items appear in the workspace:

| Item     | Name (default)                  | Type     |
|----------|---------------------------------|----------|
| Notebook | `nb_02_train_univariate_ae`     | Notebook |
| Notebook | `nb_03_train_multivariate_ae`   | Notebook |

Item names use underscores throughout because some Fabric item types
(Eventstream, Reflex, …) reject hyphens. Defaults can be overridden in
`.env`.

The script is **idempotent**: re-running skips items that already exist.

## Adding more items

Add a line in `scripts/deploy.ps1`, e.g.:

```powershell
New-FabricItem -Workspace $ws -Name 'my_model' -Type MLModel
```

To import a notebook / pipeline / semantic model from source, drop a
`items/<name>.<Type>/` definition folder and call:

```powershell
Import-FabricItem -Workspace $ws -Path 'items/my_model.SemanticModel'
```

## CI / non-interactive use

For pipelines, switch authentication to a service principal — set these
as repo/org secrets (never commit them):

```powershell
fab auth login `
  --tenant        $env:FABRIC_TENANT_ID `
  --client-id     $env:FABRIC_CLIENT_ID `
  --client-secret $env:FABRIC_CLIENT_SECRET
```
