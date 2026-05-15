# Current state

_Last updated: 2026-05-16_

## Where we are

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
`notebooks/06_simulator_dev.ipynb`, 5 machines × 24 h @ 1 Hz,
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
