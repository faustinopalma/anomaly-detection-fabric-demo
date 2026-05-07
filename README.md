# anomaly-detection-fabric-demo

Bootstrap a Microsoft Fabric workspace and core items (Lakehouse, Notebook,
Data Pipeline, Eventhouse + KQL Database) using the **Fabric CLI** (`fab`)
driven from PowerShell, with **device-code authentication**.

## Prerequisites

- Windows / macOS / Linux with [PowerShell 7+](https://learn.microsoft.com/powershell/scripting/install/installing-powershell)
- Python 3.9+ (for `pip install ms-fabric-cli`)
- An existing Fabric **capacity** you can assign workspaces to
- An Entra account (or service principal) with rights on that capacity
- Tenant admin must have enabled service principals / users to call Fabric APIs

## Setup

```powershell
# 1. Install the Fabric CLI (once)
pip install --upgrade ms-fabric-cli

# 2. Configure local secrets
Copy-Item .env.example .env
# edit .env and fill in your tenant id, capacity name, workspace name, etc.

# 3. Run the bootstrap script
./scripts/deploy.ps1
```

The first run launches a **device-code login** in your browser. The token is
cached under `~/.config/fab/` (gitignored) so subsequent runs are silent
until it expires.

## Layout

```
.
├── .env.example              # template; copy to .env (gitignored)
├── .gitignore
├── README.md
├── items/
│   └── ingest.Notebook/      # notebook definition imported into the workspace
│       ├── .platform
│       └── notebook-content.py
└── scripts/
    ├── deploy.ps1            # main entrypoint
    └── lib/
        ├── env.ps1           # .env loader + validation
        └── fabric.ps1        # thin helpers around `fab`
```

## What the script creates

| Item                | Name (default)         | Notes                                              |
|---------------------|------------------------|----------------------------------------------------|
| Workspace           | `$FABRIC_WORKSPACE_NAME` | Assigned to `$FABRIC_CAPACITY_NAME`              |
| Lakehouse           | `bronze`               | Empty, schema-enabled default                      |
| Notebook            | `ingest`               | Imported from `items/ingest.Notebook/`             |
| Data Pipeline       | `daily_load`           | Empty pipeline (edit in the portal or via import)  |
| Eventhouse          | `telemetry`            | Hosts the KQL database                             |
| KQL Database        | `signals`              | Inside the `telemetry` Eventhouse                  |

The script is **idempotent**: re-running it updates existing items instead of
failing.

## Adding more items

Drop a definition folder under `items/<name>.<ItemType>/` and add a line in
`scripts/deploy.ps1`:

```powershell
Import-FabricItem -Workspace $ws -Path "items/my_model.SemanticModel"
```

## CI / non-interactive use

For pipelines, switch authentication to a service principal:

```powershell
fab config set auth.mode service_principal
fab auth login `
  --tenant      $env:FABRIC_TENANT_ID `
  --client-id   $env:FABRIC_CLIENT_ID `
  --client-secret $env:FABRIC_CLIENT_SECRET
```

Store those values as repo/organization secrets — never commit them.
