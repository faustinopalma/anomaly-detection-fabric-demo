# Plan

_Last updated: 2026-06-02 (Task A done; Task B SWA control panel done — live deploy is manual)_

## DONE — Task B: Static Web App control panel for the simulator

Goal (user request): add a Static Web App connected to the cloud simulator
container. The SWA shows each simulated machine's **state** (simple, not
choreographic), lets the operator **toggle per-machine random-anomaly mode**,
and when random mode is OFF lets them **inject anomalies manually**. The SWA
sends commands to the container; if the container is down (stopped to save
cost) the SWA shows it and stays inactive.

Architecture decision: the container today is a one-way producer (no inbound
API). Added a small **FastAPI control server** inside the simulator process
(background thread) exposing a read-only state snapshot + control endpoints,
secured with an `X-API-Key` header. A static (no-build) HTML/JS front-end
hosted on Azure Static Web Apps (free tier) polls `/api/state` and POSTs
control commands.

Sub-steps (all tested + committed + pushed):
1. ✅ `control.py` — thread-safe `ControlState`. Commit `11d4dee`.
2. ✅ `ControlState` threaded through `run()`/`main()` + `--dry-run`.
   Commit `82aa841`.
3. ✅ `server.py` FastAPI app + `cloud_runner` wiring + requirements +
   `.env.example`. Commit `a22f9d5`.
4. ✅ `webapp/` static front-end + `staticwebapp.config.json` + README.
   Commit `19c0284`.
5. ✅ `infra/swa.bicep` (Free SKU, `bicep build` OK). Commit `5e19c66`.
6. ✅ `webapp/deploy.ps1` local deploy helper + local e2e test over real
   HTTP (dry-run sim + uvicorn). Commit `f357afd`.

**Manual step left for the user (never modify live infra unattended):**
enable external ingress on `ca-simulator` for `SIM_CONTROL_PORT` and set
`SIM_CONTROL_ENABLED=1` + `SIM_CONTROL_API_KEY` (+ CORS origins), redeploy
the container, then run `webapp/deploy.ps1`. Commands documented in
`infra/swa.bicep` and `webapp/README.md`.

## DONE — Task A: docs & dashboard audit

Confirmed live fleet = 3 machines (M-004 trained-only, not wired live).
Corrected `README.md`, `docs/architecture.md`,
`docs/model_architecture_options.md`, `tools/README.md`. Added 3 CNC
timecharts to `tools/04_create_dashboard.py` and applied to the live
`rtd_telemetry_live` dashboard. Commits `23fefc2`, `91d6eb4`.

---

_Earlier: 2026-06-01 (M-003 real-data CNC machine added and validated)_

## DONE — 3rd machine (M-003, real-data CNC)

Completed 2026-06-01. A third machine was added from the real CNC data in
`_data_local/`:

1. Built a recorded CNC profile (`data/cnc_profile_M-003.json`) and a
   `CNCMachine` simulator path (3 sensors: load/power/torque).
2. Trained `transformer_ae_small__M-003` (single-file FP16 ONNX,
   161 419 params, threshold 1.882) and registered it to the live `models`
   table (version 1).
3. Wired `fn_score_demo_M003()` into `kql/04_update_policy.kql` and applied
   it live. Deployed the cloud simulator at `SIM_MACHINES=3` with
   `SIM_CNC_PROFILE` (revision `ca-simulator--v2606011249`).
4. Found and fixed the real cause that ingestion had stopped on May 27: the
   eventstream destination `kql_raw_telemetry` was **Paused**. Resumed via
   `tools/_check_topology.py`.
5. Validated end-to-end: M-003 ingests (3 sensors), scores ~1.17 on normal
   data, and an injected drift produced an anomaly at score 9.79.

Remaining: run a longer live correlation to confirm precision/recall once
enough natural anomalies accumulate (see STATE Outstanding #1).

## Phase 1 — Validate the per-machine pipeline (historical)

1. Wait ~15 min for fresh `M-001`/`M-002` data to accumulate after the
   simulator restart (revision `ca-simulator--0000001` in
   `rg-fabric-demo`).
2. Quick check that BOTH machines are detecting anomalies under the new
   policies:
   ```kql
   anomalies
   | where detected_at > ago(15m)
   | summarize n=count() by model_name, machine_id
   ```
   Expect rows for both `transformer_ae_small__M-001` and
   `transformer_ae_small__M-002`.
3. Run live correlation:
   ```powershell
   .\.venv\Scripts\python.exe tools\06_correlate.py --lookback 30m --grace 2m
   ```
   Per-machine targets (each, not aggregate):
   - Precision >= 95%
   - Recall >= 60%

## Phase 2 — If recall is low (< 60% on either machine)

Options to try, cheapest first:

A. **Lower threshold quantile.** Retrain (~30 s each) with
   `--threshold-quantile 0.99` and re-register. No code or KQL changes.

B. **Longer training data.** Currently each per-machine model sees
   ~420k rows = ~5 days. If recall on slow drifts is the issue,
   either:
   - Regenerate training parquet in `notebooks/01_simulator_dev.ipynb`
     with `DURATION_S = 10 * 24 * 3600`, or
   - Accept the limit and move on.

C. **Longer window.** Change `WINDOW = 64` to `128` or `256` in
   `tools/train_per_machine.py`. Slower drifts become visible. Bigger
   ONNX — verify it still fits the 1 MB Kusto row budget (it will at
   WINDOW=128; check at WINDOW=256).

D. **Per-sensor anomaly subscores.** The score is already
   `max-over-features(MSE)` so this is already in effect.

## Phase 3 — Demo-ready cleanup (after validation passes)

1. Restore production anomaly rate:
   ```powershell
   az containerapp update -g rg-fabric-demo -n ca-simulator `
       --set-env-vars SIM_ANOMALY_PROB=0.0005
   ```
2. (Optional) Delete the legacy container app + its ACR:
   ```powershell
   az containerapp delete -g fabric-anomaly-detection -n ca-simulator --yes
   az acr delete -n acrsim3l8kge --yes
   ```
3. Commit and push. Suggested message:
   `feat: per-machine architecture (2 machines, 2 dedicated ONNX models)`

## Phase 4 — Documentation (after Phase 3)

1. Update `README.md` and `docs/architecture.md` with the per-machine
   topology diagram.
2. Update `docs/RUNBOOK.md` to reference `tools/train_per_machine.py`
   and the per-machine model naming convention.
3. Notebooks `01_simulator_dev.ipynb` and `06_train_transformer_small.ipynb`
   were intentionally NOT rewritten — they remain valid for offline
   experimentation but the production path is now the script + KQL.
   Note this near the top of each notebook.

## Known gotchas / non-goals
- We did NOT regenerate the training parquet. The existing
  `data/training/telemetry_wide.parquet` (10 machines × 5 days) is
  filtered per machine at training time. This is intentional: the
  existing data is fine and regeneration burns ~10 min.
- We did NOT run the offline eval (`data/eval/*`) because the eval set
  uses machine IDs `M-101..M-108` that no longer correspond to the
  production fleet. PR-AUC is no longer computed; we trust live
  correlation as the operational metric.
- `.purge` on KQL tables requires Fabric Eventhouse admin
  authorization that our principal does not have. Old rows for
  M-003..M-005 will age out naturally with the 30-day softdelete
  retention. `tools/purge_obsolete_machines.py` exists but currently
  errors with 403; keep it as a one-shot for the day someone grants
  admin rights.
