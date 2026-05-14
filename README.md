# anomaly-detection-fabric-demo

Bootstrap a Microsoft Fabric workspace for a **factory anomaly-detection
demo** using the **Fabric CLI** (`fab`) driven from PowerShell with
**device-code authentication**.

The demo ingests time-series telemetry from multiple machines (each with
multiple sensors), trains a window-based model offline, exports it to
**ONNX**, and scores it **inside the Fabric KQL database** via the
`python()` plugin — no external Spark/AKS cluster required.

## Documentation

Read in this order, depending on what you want:

| Doc | What you get |
|---|---|
| [`docs/concepts.md`](docs/concepts.md) | Plain-English tour of the architecture and the design choices behind it. **Start here.** |
| [`docs/architecture.md`](docs/architecture.md) | Deployed pieces of this demo (items, names, post-deploy steps). |
| [`docs/anomaly_detection_fabric_kql.md`](docs/anomaly_detection_fabric_kql.md) | KQL cookbook: every available path for in-Eventhouse anomaly detection, with code. |
| [`docs/data_modeling_industrial_measures.md`](docs/data_modeling_industrial_measures.md) | How to shape tables when measurements come in heterogeneously (long vs wide vs hybrid). |
| [`tools/README.md`](tools/README.md) | Local simulator + CLI helpers used to set up Eventstream and run KQL scripts. |

## Prerequisites

- Windows / macOS / Linux with [PowerShell 7+](https://learn.microsoft.com/powershell/scripting/install/installing-powershell)
- Python 3.10+ (for `pip install ms-fabric-cli`)
- An existing Fabric **capacity** you can assign workspaces to
- An Entra account with rights on that capacity
- Tenant admin must have enabled "Users can use Fabric APIs"
- For the in-KQL ONNX scoring: the `python()` plugin enabled on the
  Eventhouse (admin toggle)

## Setup

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

```
.
├── .env.example                          # template; copy to .env (gitignored)
├── README.md
├── docs/
│   ├── concepts.md                              # plain-English tour — start here
│   ├── architecture.md                          # deployed items + post-deploy steps
│   ├── anomaly_detection_fabric_kql.md          # KQL cookbook (every option, with code)
│   └── data_modeling_industrial_measures.md     # long vs wide vs hybrid table designs
├── kql/
│   ├── 01_tables.kql                     # raw_telemetry, anomalies, batching policy, streaming OFF
│   ├── 02_models.kql                     # versioned ONNX model registry
│   ├── 03_scoring_functions.kql          # univariate + multivariate window builders, python(onnx) scorers
│   ├── 04_update_policy.kql              # auto-score on ingest (univariate)
│   └── 05_multivariate_mv.kql            # wide materialized view + multivariate scoring + 2nd update policy
├── items/                                # blank scaffolds, kept for the legacy notebooks named below
│   ├── nb_prepare_features.Notebook/         # legacy — superseded by the wide MV
│   ├── nb_train_export_onnx.Notebook/        # legacy — superseded by notebooks/04 and 05
│   └── nb_register_kql_scorer.Notebook/      # still in use: re-applies kql/*.kql
├── notebooks/                            # active training notebooks (publish via tools/upload_notebook.py)
│   ├── 04_train_univariate_ae.ipynb      # per-sensor LSTM AE → univariate_ae__<sensor_id>
│   └── 05_train_multivariate_ae.ipynb    # per-machine LSTM AE over wide MV → multivariate_ae__<machine_id>
├── tools/                                # Python helpers (Eventstream wiring, KQL setup, anomaly inject, notebook publish)
├── simulator-local/                      # run the simulator locally
├── simulator-cloud/                      # always-on simulator on Azure Container Apps
└── scripts/
    ├── deploy.ps1                        # main entrypoint
    └── lib/
        ├── env.ps1                      # .env loader + validation
        └── fabric.ps1                   # thin idempotent helpers around `fab`
```

## What the script creates

All items below are **blank container items** — the legacy `nb_*` notebooks
ship with starter scaffolds from `items/`. The active training notebooks
(`04_train_univariate_ae`, `05_train_multivariate_ae`) live under
`notebooks/` and are published as Fabric Notebook items separately with
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
| Notebook         | `nb_prepare_features`     | Notebook (legacy scaffold) |
| Notebook         | `nb_train_export_onnx`    | Notebook (legacy scaffold) |
| Notebook         | `nb_register_kql_scorer`  | Notebook       |
| Data Pipeline    | `pl_retrain`              | DataPipeline   |
| Reflex           | `act_anomaly_alerts`      | Reflex         |
| Semantic Model   | `sm_anomaly`              | SemanticModel  |
| Report           | `rpt_anomaly`             | Report         |

In addition, after running the training notebooks once, two more Notebook
items appear in the workspace:

| Item     | Name (default)                  | Type     |
|----------|---------------------------------|----------|
| Notebook | `nb_04_train_univariate_ae`     | Notebook |
| Notebook | `nb_05_train_multivariate_ae`   | Notebook |

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
