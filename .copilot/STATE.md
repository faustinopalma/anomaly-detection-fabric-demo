# Current state

_Last updated: 2026-05-18 (session closed, resume tomorrow)_

## Where we are

### Fabric environment bootstrap — COMPLETE ✅ (workspace: anomaly-detection-fresh)

All Fabric items provisioned in workspace `anomaly-detection-fresh`
(capacity: `anomalydetectiondemo` F4, italynorth).

**Root cause fixed**: `fab create -P parentEventhouseItemId=<id>` puts the
param at the top level of the REST body; Fabric requires it inside
`creationPayload`. Fixed by replacing `New-FabricItem` for KQLDatabase with
a new `New-FabricKQLDatabase` function in `scripts/lib/fabric.ps1` that calls
`POST /v1/workspaces/{wsId}/kqlDatabases` directly via `Invoke-WebRequest` +
`az account get-access-token`.

**Confirmed via REST API**: `kql_telemetry.KQLDatabase` (142c5513)
`parentEventhouseItemId = bbd8cd68` = `eh_telemetry.Eventhouse` ✓

**Current workspace items** (anomaly-detection-fresh, wsId 35627f40):
- `eh_telemetry.Eventhouse` (bbd8cd68) — contains:
  - `eh_telemetry.KQLDatabase` (f52102e6) — Fabric auto-default DB
  - `kql_telemetry.KQLDatabase` (142c5513) — our DB ✓
- `lh_telemetry.Lakehouse` (cf016893)
- `es_machines.Eventstream` (dd6f7640)
- `env_anomaly.Environment` (c758b771)
- `nb_register_kql_scorer.Notebook` (38c6b6ba)
- `nb_02_train_univariate_ae.Notebook` (8b0e8083)
- `nb_03_train_multivariate_ae.Notebook` (0647c417)
- `pl_retrain.DataPipeline` (960839f2)
- `act_anomaly_alerts.Reflex` (77218e33)
- `sm_anomaly.SemanticModel` (f2fafd87)
- `rpt_anomaly.Report` (8ad9f865)
- (also `rpt_anomaly_auto.SemanticModel` — auto-created by Fabric with the Report)

**Still pending (manual steps)**:
- Enable Python plugin on `eh_telemetry.Eventhouse` in portal → Settings → Python plugin
- After enabling: apply KQL scripts 03-05 via `tools/02_setup_kql_tables.py`
- Wire Eventstream destination to `kql_telemetry.kql_raw_telemetry` table

### Scripts fixed in this session

- `scripts/lib/fabric.ps1`: added `New-FabricKQLDatabase` (REST API direct call)
- `scripts/deploy.ps1`: uses `New-FabricKQLDatabase` instead of `New-FabricItem` for KQLDatabase
- `tools/_fabric_auth.py`: prefers `AzureCliCredential` over DeviceCode



Two **versioned offline snapshots** are now committed:

**Training (clean)** — `data/training/` (commit `fdb5ce1`,
5 machines × 24 h @ 1 Hz, seed `RNG_SEED`, machines `M-001..M-005`):
- `raw_telemetry.parquet` (~32 MB, 3 456 000 rows) — long form
  `{machineId, sensorId, ts, value, quality}` mirroring KQL `raw_telemetry`.
- `telemetry_wide.parquet` (~20 MB, 432 000 rows) — pivoted
  `{ts, machineId, state, load, 8 sensors}` with ground-truth `state`.
- `sample_head.csv` — 200-row PR-friendly sample.
- Codec: zstd lvl 9, row group 50k. **Train on this snapshot only.**

**Eval (with anomalies)** — `data/eval/` (Section 8 of
`notebooks/01_simulator_dev.ipynb`, 5 machines × 24 h @ 1 Hz,
seed `RNG_SEED+1000`, machines `M-101..M-105`):
- `raw_telemetry.parquet` (~32 MB) and `telemetry_wide.parquet` (~20 MB)
  with the **same schema** as training, plus `is_anomaly` + `fault_type`
  in the wide form.
- `anomaly_labels.parquet` — 12-row episode catalog
  `{episode_id, machine_id, fault_type, onset_ts, end_ts, duration_s,
    severity_max, affected_sensor, pattern, notes}` (ground truth).
- 12 episodes × 3 fault families, all on dedicated machines so
  M-101/M-102 stay clean as eval-time normals:
    - `bearing` on M-103 (4 episodes, severity 0.30 → 1.00, ramp
      degradation on vibrations + bearing temp + current/power +
      load-scaled Poisson spikes).
    - `hydraulic_leak` on M-104 (4 episodes, mix of `ramp` slow leak
      and `oscillation` 60 s pump duty-cycling on `pressure_hydraulic`,
      with small power compensation).
    - `sensor_stuck` on M-105 (4 episodes on
      `temperature_motor`/`pressure_hydraulic`/`vibration_radial`/`current`,
      sensor frozen at the last pre-onset value, `quality=0` in the
      long form).
- Schema is identical to training so the same model code runs against
  both, and later against `spark.read.kusto(...)` in Fabric without
  changes.



The **Fabric capacity provisioning** workstream is **complete**:

- `infra/fabric-capacity.bicep` — Bicep template for `Microsoft.Fabric/capacities`.
- `scripts/create-capacity.ps1` — wrapper that reads `.env` (via shared
  `scripts/lib/env.ps1`), uses device-code auth, defaults to F4 in
  `italynorth`, and runs the Bicep deployment.
- Pushed in commit `5c9f196` on `main`.

The **simulator + training redesign** — Phase 1 (physics simulator) is
**validated in a sandbox notebook**:

- `notebooks/01_simulator_dev.ipynb` runs end-to-end. User confirmed
  2026-05-15 that "il simulatore funziona bene, i grafici non sono
  affatto male."
- Bug fixed during validation: `np.random.choice` was casting `State`
  enum members to a fixed-length numpy string array and truncating the
  longer names (`State.STARTUP` → `'State.S'`). Fixed by picking an
  index instead and indexing the original tuple.
- Phases 2-4 not started; six open questions in `PLAN.md` still pending.

## Active focus

Next candidate steps (pick one):
1. Open a new `notebooks/07_train_offline.ipynb` that loads
   `data/training/telemetry_wide.parquet`, trains on clean only, and
   evaluates against `data/eval/telemetry_wide.parquet` using
   `data/eval/anomaly_labels.parquet` as ground truth (PR-AUC,
   per-fault-family detection delay).
2. Port the validated simulator + injectors from the notebook into
   `simulator-local/simulate_machines.py` (preserve CLI + JSON payload
   for streaming into the Fabric eventstream).
3. Tune simulator coefficients further (vibrations vs jitter, thermal
   max temp, IDLE/OFF mix) and regenerate both snapshots.

The 6 open questions in `PLAN.md` still block Phases 2-4.

## Recent context the user might mention

- Bootstrap prep for a brand-new environment was started on 2026-05-18:
  - Local preflight passed for required files and tools except `fab`.
  - `ms-fabric-cli` was installed (`fab version 0.1.10`).
  - `.env` was created from `.env.example` (it still needs real tenant/
    subscription/capacity values before running provisioning scripts).
  - Dedicated local Python environment `.venv` was created and validated.
    Installed from `tools/requirements-sim.txt` and
    `simulator-local/requirements.txt`, plus `ms-fabric-cli` in-venv.
    Smoke imports pass (`torch`, `onnx`, `onnxruntime`, `pyarrow`,
    `matplotlib`, `pandas`) and `fab` is available in
    `.venv\Scripts\fab.exe`.
  - New Fabric capacity successfully created via
    `scripts/create-capacity.ps1`:
    - name: `anomalydetectiondemo`
    - SKU/location: `F4` / `italynorth`
    - resource group: `rg-fabric-demo`
    - `.env` aligned to `FABRIC_CAPACITY_NAME=anomalydetectiondemo`
  - `scripts/deploy.ps1` now completes successfully on this environment
    (workspace/items idempotent, all currently skipped as already present).
  - Robustness fixes applied:
    - `scripts/lib/fabric.ps1` now resolves `fab` from `.venv\Scripts\fab.exe`
      when not in PATH.
    - `Get-FabricItemId` now reads IDs via `fab ls -l <workspace>` parsing,
      avoiding `fab get -q id` failures for some item types.
    - UTF-8 enforced for Fabric CLI calls in `scripts/lib/fabric.ps1` and
      `scripts/deploy.ps1` to avoid charmap failures on unicode output.
  - Post-deploy bootstrap is in progress; blocked on first-run device-code
    auth for Python Fabric REST helpers (`tools/01_setup_eventstream_source.py`).

## Latest milestone (2026-05-18)

Bootstrap from a new Fabric capacity is now completed end-to-end.

- Root cause of `CapacityNotActive` was identified and fixed: workspace
  `anomaly-detection-dev` was still assigned to an older capacity GUID.
  Reassigned with:
  - `fab assign .capacities/anomalydetectiondemo.Capacity -W /anomaly-detection-dev.Workspace -f`
- Post-deploy automation succeeded:
  - `tools/01_setup_eventstream_source.py` (source + connection string in
    `.env`)
  - `tools/02_setup_kql_tables.py` with `kql/01..05` applied
  - `tools/03_setup_eventstream_destination.py`
  - `tools/upload_notebook.py notebooks/02_train_univariate_ae.ipynb`
  - `tools/upload_notebook.py notebooks/03_train_multivariate_ae.ipynb`

- The user often works across two machines via VS Code Remote Tunnels.
  Chat history does not sync. That's why this folder exists.
- The current Fabric environment (capacity `anomalydetection`, workspace
  `anomaly-detection-dev`) must **not** be modified without explicit
  confirmation — fixes go in scripts/code only.
- A previous deploy bug (KQL DB linked to a wrong Eventhouse via
  `parentEventhouseName`, creating a `<dbname>_auto` orphan) was fixed
  in `scripts/deploy.ps1` (commit `be48112`) by switching to
  `parentEventhouseItemId=<GUID>` lookup. The orphan in the live env was
  left in place on purpose.

## Not yet done (carry-over)

- Test the fixed `scripts/deploy.ps1` on a fresh capacity (the user can
  now provision one with `pwsh ./scripts/create-capacity.ps1`).
- GPU patches in notebook 05 (`device = torch.device('cuda' if ...)`).
  The user has tunneling set up but hasn't asked for the patch yet.
