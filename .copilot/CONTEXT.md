# Stable context

Long-lived facts that the user shouldn't have to re-explain to a fresh
Copilot session. Update only when these facts actually change.

## Repository map

| Path | Purpose |
|---|---|
| `simulator-local/simulate_machines.py` | Local synthetic telemetry generator. Sends to Eventstream via the Event Hubs–compatible custom endpoint. Reads `EVENTSTREAM_CONNECTION_STRING` from `.env`. |
| `simulator-cloud/` | Same simulator, packaged for always-on Azure Container Apps deployment. |
| `notebooks/01_simulator_dev.ipynb` | Physics simulator + offline dataset builder (`data/training/`, `data/eval/`). |
| `notebooks/02_train_univariate_ae.ipynb` | Per-sensor LSTM AE (one model per sensor). |
| `notebooks/03_train_multivariate_ae.ipynb` | Per-machine multivariate LSTM AE over the wide MV. Currently 1×LSTM enc + 1×LSTM dec, threshold = μ + K·σ. |
| `tools/` | Python helpers: Eventstream wiring (`03_setup_eventstream_destination.py`), KQL setup, anomaly injection, notebook publishing. |
| `scripts/deploy.ps1` | Main idempotent deploy: workspace + items on an existing Fabric capacity. |
| `scripts/create-capacity.ps1` | One-shot Bicep deployment of a new Fabric capacity. |
| `scripts/lib/env.ps1` | Shared `.env` loader (`Import-DotEnv`) and `Assert-EnvVars`. **Always reuse this**, do not reimplement inline. |
| `scripts/lib/fabric.ps1` | Idempotent helpers around the `fab` CLI. Includes `Get-FabricItemId` for resolving GUIDs. |
| `infra/fabric-capacity.bicep` | `Microsoft.Fabric/capacities@2023-11-01` template. |
| `docs/` | User-facing documentation (architecture, KQL pipeline, modeling rationale, concepts). |
| `.copilot/` | Session handoff (this folder). |

## Live Fabric environment (do **not** modify)

- Tenant: `39d764bc-ae80-46f9-b22c-6246cc5a20c2`
- Capacity: `anomalydetection`  (F4)
- Workspace: `anomaly-detection-dev`  (id `19358f48-64ff-4cd0-8189-48e8ab77768c`)
- Active Eventhouse: `kql_telemetry_auto`  (id `7849f0e3-9583-4ab3-bb2c-7b1e06d31cb0`)
- Active KQL DB: `kql_telemetry`  (inside the above; id `6130751d-3e86-4b72-8d34-6b3ec11e23e1`)
- Kusto URI: `https://trd-xr97y56tuzzkxy5cgp.z5.kusto.fabric.microsoft.com`
- Orphan (do not delete without asking): Eventhouse `eh_telemetry`
  (id `27eec04d-82f4-4161-87fa-7770737ada90`).

## Sensor catalogue (8 channels per machine, 1 Hz)

| Sensor id | Unit | Baseline |
|---|---|---|
| `temperature_motor` | °C | 60 |
| `temperature_bearing` | °C | 55 |
| `vibration_axial` | g | 0.20 |
| `vibration_radial` | g | 0.30 |
| `current` | A | 12 |
| `spindle_rpm` | rpm | 3000 |
| `pressure_hydraulic` | bar | 120 |
| `power` | kW | 8 |

These ids must stay stable: KQL update policies, the wide MV column order,
and the ONNX model input ordering all depend on them. Any reorder is a
breaking change.

## Conventions

- Event payload is `{machineId, sensorId, ts, value, quality}`. `ts` is
  ISO-8601 UTC with microseconds, `Z`-suffixed.
- Item names use **underscores** throughout (some Fabric item types reject
  hyphens). E.g. `kql_telemetry`, `es_machines`, `nb_register_kql_scorer`.
- KQL DB ↔ Eventhouse linking via REST API requires the parent
  Eventhouse's **item id** (GUID), not its name. Passing
  `parentEventhouseName` is silently ignored and Fabric auto-creates a
  `<dbname>_auto` Eventhouse — already a documented gotcha in
  `scripts/deploy.ps1`.
- All Python: type hints, `from __future__ import annotations`, PEP 8.
- All PowerShell scripts: dot-source `scripts/lib/env.ps1`, never roll
  your own `.env` parser.

## Known constraints / gotchas

- KQL **materialized views cannot host update policies** — that's why
  the trigger lives on `raw_telemetry` and the MV is referenced from
  inside the policy (with anchor-sensor dedup to avoid double scoring).
- The Eventstream custom endpoint is Event Hubs–compatible; we use the
  `azure-eventhub` SDK directly in the simulator.
- Fabric's `fab` CLI sometimes prefixes its `-q id` output with `* `
  (TTY marker). `Get-FabricItemId` strips it before validating the GUID.
