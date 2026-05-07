# anomaly-detection-fabric-demo

Bootstrap a Microsoft Fabric workspace for a **factory anomaly-detection
demo** using the **Fabric CLI** (`fab`) driven from PowerShell with
**device-code authentication**.

The demo ingests time-series telemetry from multiple machines (each with
multiple sensors), trains a window-based model offline, exports it to
**ONNX**, and scores it **inside the Fabric KQL database** via the
`python()` plugin — no external Spark/AKS cluster required.

> See [`docs/architecture.md`](docs/architecture.md) for the full design
> (data flow, KQL schema, training loop, constraints).

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
├── .env.example                  # template; copy to .env (gitignored)
├── .gitignore
├── README.md
├── docs/
│   └── architecture.md           # full design + diagrams
├── kql/
│   ├── 01_tables.kql             # raw_telemetry, anomalies
│   ├── 02_models.kql             # versioned ONNX model registry
│   ├── 03_scoring_functions.kql  # window builders + python(onnx) scorers
│   └── 04_update_policy.kql      # auto-score on ingest
├── items/
│   ├── nb-prepare-features.Notebook/
│   ├── nb-train-export-onnx.Notebook/
│   └── nb-register-kql-scorer.Notebook/
└── scripts/
    ├── deploy.ps1                # main entrypoint
    └── lib/
        ├── env.ps1               # .env loader + validation
        └── fabric.ps1            # thin idempotent helpers around `fab`
```

## What the script creates

All items are **blank** (notebooks ship with starter code from `items/`).
Schema, Eventstream sources/destinations, and Reflex rules are configured
post-deploy — see [`docs/architecture.md`](docs/architecture.md).

| Item             | Name (default)            | Type           |
|------------------|---------------------------|----------------|
| Workspace        | `anomaly-detection-dev`   | Workspace      |
| Eventstream      | `es-machines`             | Eventstream    |
| Eventhouse       | `eh-telemetry`            | Eventhouse     |
| KQL Database     | `kql-telemetry`           | KQLDatabase    |
| Lakehouse        | `lh-telemetry`            | Lakehouse      |
| Environment      | `env-anomaly`             | Environment    |
| Notebook         | `nb-prepare-features`     | Notebook       |
| Notebook         | `nb-train-export-onnx`    | Notebook       |
| Notebook         | `nb-register-kql-scorer`  | Notebook       |
| Data Pipeline    | `pl-retrain`              | DataPipeline   |
| Reflex           | `act-anomaly-alerts`      | Reflex         |
| Semantic Model   | `sm-anomaly`              | SemanticModel  |
| Report           | `rpt-anomaly`             | Report         |

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
