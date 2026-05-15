# Runbook — fresh-machine reproduction

End-to-end checklist to take a brand-new PC (or a clean VS Code Remote
Tunnel) from `git clone` to a working Fabric environment ingesting live
telemetry from the local simulator. **Run the steps in order.** Each step
is idempotent unless noted otherwise.

For the *why* behind each piece read [`concepts.md`](concepts.md) and
[`architecture.md`](architecture.md); this file is just the recipe.

---

## 0. Prerequisites on the local box

Install once per machine:

- **Git** ≥ 2.40
- **PowerShell 7+** (`pwsh`)
- **Python 3.10+** (3.13 is what the project is developed against)
- **Azure CLI** — `winget install Microsoft.AzureCLI` (Windows) or
  [docs](https://learn.microsoft.com/cli/azure/install-azure-cli)
- An Entra account with:
    - rights to create resources in the target Azure subscription
      (Contributor on the resource group is enough), and
    - access to the Fabric tenant where the workspace will live.
- Tenant admin must have toggled **"Users can use Fabric APIs"** on.
- For in-KQL ONNX scoring the **`python()` plugin** must be enabled on
  the Eventhouse (admin toggle, one-off).

Hardcoded path policy: **never print absolute paths in scripts or
notebook outputs** — use repo-relative paths only.

---

## 1. Clone and configure

```pwsh
git clone https://github.com/faustinopalma/anomaly-detection-fabric-demo.git
cd anomaly-detection-fabric-demo

# Copy the env template and fill it in. .env is gitignored.
Copy-Item .env.example .env
# Edit .env — at minimum set:
#   FABRIC_TENANT_ID, FABRIC_CAPACITY_NAME, FABRIC_WORKSPACE_NAME,
#   AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, AZURE_LOCATION,
#   FABRIC_CAPACITY_SKU, FABRIC_CAPACITY_ADMINS
```

---

## 2. Python environment

```pwsh
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r tools/requirements-sim.txt
pip install --upgrade ms-fabric-cli   # `fab` CLI used by deploy.ps1
```

Quick check:

```pwsh
fab --version
python -c "import torch, onnx, onnxruntime, pyarrow, matplotlib, pandas; print('ok')"
```

---

## 3. Azure login (capacity provisioning only)

```pwsh
az login --tenant $env:FABRIC_TENANT_ID
az account set --subscription $env:AZURE_SUBSCRIPTION_ID
az provider register --namespace Microsoft.Fabric    # one-off per subscription
```

---

## 4. Provision the Fabric capacity

Skip if `FABRIC_CAPACITY_NAME` already points to an existing active
capacity you have rights on.

```pwsh
pwsh ./scripts/create-capacity.ps1
```

Reads everything from `.env`. Creates the resource group if missing and
deploys `infra/fabric-capacity.bicep`. Prints the capacity name when done.

---

## 5. Provision the workspace and items

```pwsh
pwsh ./scripts/deploy.ps1
```

First run launches a **device-code login** in the browser (Fabric CLI
token cached under `~/.config/fab/`). Creates the workspace and all
container items listed in [`architecture.md` §3](architecture.md#3-items-provisioned-by-scriptsdeployps1).
Re-runs are idempotent: existing items are skipped.

---

## 6. Wire the Eventstream Custom Endpoint (manual, portal)

`scripts/deploy.ps1` creates an empty Eventstream. To receive data from
the simulator add a Custom App source once:

1. Fabric portal → workspace `anomaly-detection-dev` →
   `es_machines.Eventstream`.
2. **+ Add source → Custom App** → name `sim_local` → **Add**.
3. Click the new `sim_local` node → **Details → Event Hub → SAS Key
   Authentication** → copy **Connection string-primary key**.
4. Paste it into `.env`:

   ```
   EVENTSTREAM_CONNECTION_STRING=Endpoint=sb://...;EntityPath=es_...
   ```

(See [`tools/README.md`](../tools/README.md) for the field-by-field
walkthrough.)

---

## 7. Apply the KQL schema

```pwsh
python tools/02_setup_kql_tables.py `
    kql/01_tables.kql `
    kql/02_models.kql `
    kql/03_scoring_functions.kql `
    kql/04_update_policy.kql `
    kql/05_multivariate_mv.kql
```

Auth uses the same `.env` as `deploy.ps1`. Order matters — apply files in
numeric order. See [`architecture.md` §4.2](architecture.md#42-kql-schema)
for what each file installs.

---

## 8. Wire Eventstream destinations

```pwsh
python tools/01_setup_eventstream_source.py        # binds sim_local source if needed
python tools/03_setup_eventstream_destination.py   # adds Eventhouse + Lakehouse destinations
```

---

## 9. Upload the active training notebooks

```pwsh
python tools/upload_notebook.py notebooks/02_train_univariate_ae.ipynb
python tools/upload_notebook.py notebooks/03_train_multivariate_ae.ipynb
```

Creates `nb_02_train_univariate_ae` and `nb_03_train_multivariate_ae`
in the workspace; re-runs replace the definition in place.

---

## 10. Smoke-test ingestion with the local simulator

```pwsh
# Default: 5 machines, 1 sample/s/sensor, infinite (Ctrl-C to stop)
pip install -r simulator-local/requirements.txt
python simulator-local/simulate_machines.py --duration 60
```

Then in the Fabric portal queryset on `kql_telemetry`:

```kusto
raw_telemetry | summarize n=count(), latest=max(ts) by machineId
```

Expect 5 rows with `n > 0` and `latest` within the last minute.

---

## 11. (Optional) Rebuild the offline datasets

The repo ships pre-built parquet snapshots at:

- `data/training/` — clean baseline (5 × 24 h, machines `M-001..M-005`,
  seed `RNG_SEED`)
- `data/eval/` — same shape with 12 injected anomaly episodes (machines
  `M-101..M-105`, seed `RNG_SEED+1000`, 3 fault families)

You only need to rebuild them if you change the simulator physics, the
schema, or the anomaly catalog.

```pwsh
# Open the notebook and run all cells top-to-bottom in the .venv kernel:
code notebooks/01_simulator_dev.ipynb
# Sections 1-6 = sandbox + plots
# Section 7   = writes data/training/
# Section 8   = writes data/eval/ and anomaly_labels.parquet
```

Both snapshots use zstd lvl 9 parquet and share the **same schema** as
the live KQL `raw_telemetry` table, so any model trained on
`data/training/telemetry_wide.parquet` runs unchanged against
`spark.read.kusto(...)` in Fabric.

---

## 12. Sanity-check the resumed environment

```pwsh
git status                      # clean
git log --oneline -5            # latest commit on main
fab auth status                 # logged in
az account show -o table        # right subscription
fab workspace list              # FABRIC_WORKSPACE_NAME present
```

If any check fails, see [`STATE.md`](../.copilot/STATE.md) for the last
known-good context, [`PLAN.md`](../.copilot/PLAN.md) for outstanding
work, and [`CONTEXT.md`](../.copilot/CONTEXT.md) for stable facts that
don't change between sessions.
