# Current state

_Last updated: 2026-05-15_

## Where we are

A **versioned offline training snapshot** is now committed under
`data/training/` (commit `422cb09`):

- `raw_telemetry.parquet` — long-form events
  `{machineId, sensorId, ts, value, quality}` mirroring the live KQL
  `raw_telemetry` schema (~3.0 MB, 345 600 rows).
- `telemetry_wide.parquet` — post-pivot per-`(ts, machineId)` layout
  with 8 sensor columns + `state` (ground truth) + `load`
  (~2.1 MB, 43 200 rows).
- `sample_head.csv` — 200-row PR-friendly sample.
- Built by Section 7 of `notebooks/06_simulator_dev.ipynb`
  (3 machines × 4 h @ 1 Hz, deterministic per-machine seeds). Schema
  is **the contract**: the same model code will run later against
  `spark.read.kusto(...)` in Fabric without changes.



The **Fabric capacity provisioning** workstream is **complete**:

- `infra/fabric-capacity.bicep` — Bicep template for `Microsoft.Fabric/capacities`.
- `scripts/create-capacity.ps1` — wrapper that reads `.env` (via shared
  `scripts/lib/env.ps1`), uses device-code auth, defaults to F4 in
  `italynorth`, and runs the Bicep deployment.
- Pushed in commit `5c9f196` on `main`.

The **simulator + training redesign** — Phase 1 (physics simulator) is
**validated in a sandbox notebook**:

- `notebooks/06_simulator_dev.ipynb` runs end-to-end. User confirmed
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
   `data/training/telemetry_wide.parquet` and starts iterating on model
   architectures (windowed AE, IsolationForest baseline, etc.).
2. Port the validated simulator from the notebook into
   `simulator-local/simulate_machines.py` (preserve CLI + JSON payload).
3. Tune simulator coefficients further (vibrations vs jitter, thermal
   max temp, IDLE/OFF mix) and regenerate the dataset.

The 6 open questions in `PLAN.md` still block Phases 2-4.

## Recent context the user might mention

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
