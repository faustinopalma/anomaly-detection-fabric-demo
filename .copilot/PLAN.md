# Plan

_Last updated: 2026-06-05 (false positives FIXED via activity gate + M-001 threshold; GPU M-002 retrain pending user decision)_

## DONE — Eliminate false positives via activity gate (DEPLOYED to live KQL)

User goal (Italian): many false positives (dashed lines on the dashboard);
"fai tutto il necessario per correggere fino in fondo"; suspected all models
needed GPU retraining.

Diagnosis (tools `_inspect_detections.py`, `_inspect_telemetry_window.py`,
`_inspect_activity.py`, `_count_false_positives.py`, live KQL):

- The high-score "false positives" (5000–6500) were **idle/off-machine windows**
  (all sensors ~0). The AE only ever saw running data → off windows are OOD →
  huge reconstruction error. No threshold fixes this. GPU retraining would NOT
  have fixed the FPs (wrong premise).
- M-001 additionally had a too-low threshold (1.007) that also flagged some
  normal *running* windows (~1.1–1.6).

Outcome:
- ☑ Step 1 — Activity gate. Added scale-free `activity` =
  `nanmedian(window_mean / scaler_mean)` to both multivariate scorers in
  `kql/03_scoring_functions.kql` (typeof → `(*, score:real, activity:real)`).
  The 4 `fn_score_demo_M00X` in `kql/04_update_policy.kql` now filter
  `activity >= 0.5 and is_anomaly`. Gate 0.5 calibrated from live buckets
  (idle/partial < 0.5 carried all FPs; injections at 0.85–1.2). Deployed via
  `tools/02_setup_kql_tables.py`.
- ☑ Step 2 — M-001 threshold 1.007 → 2.5 (active-window normal max ~1.57);
  `models/transformer_ae_small__M-001/metadata.json` edited, re-registered v2.
  M-002/M-003/M-004 thresholds unchanged.
- ☑ Step 3 — Verified live (`tools/_verify_fix.py`, gate 0.5, 3h): all four
  machines 0 FP, no injections lost (M-001 0, M-002 2 TP, M-003 7 TP,
  M-004 2 TP). ~103 FP → 0.

## PENDING (user decision) — GPU retrain of M-002 synthetic trace

- AML `ImageBuildFailure` true cause = storage `allowSharedKeyAccess=false`
  blocks the build agent's shared-key SAS snapshot download; cluster has no MI.
- Options recorded for the user: (A) temp enable shared key, (B) MI + RBAC,
  (C) defer. NOT executed autonomously (security/infra change, unattended).
- M-002 works today (0 FP / 2 TP); only synthetic-trace realism (variance
  collapse) would improve. When unblocked: re-run `tools/_submit_synthgen_gpu.py`
  → `aml.download_model` → `tools/build_synth_trace.py --reuse-bundle
  --subset-days 0` → `tools/_verify_synth_trace.py --full` (PASS/WARN) →
  retrain + re-register M-002 → rebuild/redeploy simulator image.

---

## DONE (2026-06-04) — Fix detection coverage/calibration end-to-end (DEPLOYED synth5, teardown done)

User goal (Italian): M-001/M-002/M-003 "never" flagged by the model; M-004
flagged but with false positives. Investigate end-to-end and fix. Proceed
autonomously overnight; at the very end stop the container + pause the Fabric
capacity.

Diagnosis (tools `_diagnose_scoring.py`, `_calibrate_thresholds.py`,
`_test_cnc_sensitivity.py`, live KQL over 2–3 h):

- **M-001** (thr 1.007): bimodal — normal max 0.87, injections ≥52. WORKS
  (≈25 injection windows / 3 h). The user couldn't *see* them before the
  visibility fix that already shipped. No change.
- **M-004** (thr 1.471): normal score floor p50=1.14, normal max 8.36, but
  the threshold sits *inside* the normal cluster → ~50–58 % of NORMAL windows
  flagged = false positives. Real injections cluster ≥16.9. → **raised
  threshold to 12.0** (clean gap 8.36↔16.9).
- **M-002** (thr 3.603): model IS sensitive (offline test: +3σ→4.3–7.7),
  but the live injection overlay is too weak → injections rarely cross.
  Normal max 3.26 ≈ threshold → borderline FP risk. → stronger injection +
  **raised threshold to 4.0**.
- **M-003** (thr 1.882): no live window ever exceeds 1.69 → never fires. Root
  cause: the injection overlay amplitude is value-relative (`max(|v|·0.5,1)`)
  → negligible vs the CNC sensors' huge σ (torque σ=4317) and zero during
  idle. Offline test confirms the model reacts at ≥3σ additive spikes. →
  stronger injection; **kept threshold 1.882** (already excludes normal).

Root cause (M-002/M-003): `AnomalyOverlay` scaled the deviation to the
instantaneous value, fine for the O(1–100) physics sensors but invisible for
the large-σ CNC sensors and during idle.

Outcome:
- ☑ Step 1 — `simulate_machines.py`: scale-aware injection via `sensor_sigmas`
  (rolling ~120 s per-sensor σ) + `AnomalyOverlay.scale` flooring
  spike/drift/stuck amplitude at `SIGMA_K·σ`. Committed.
- ☑ Step 2 — M-002 threshold 3.6025→4.0 (v3), M-004 1.4709→12.0 (v2),
  re-registered via `tools/05_register_model.py`. Committed.
- ☑ Step 2b — multivariate window-dilution fix: `BASE_DURATION` spike 5→28 s,
  drift 14→22, stuck 12→20; `SPIKE_SIGMA_K=1.6`, `DRIFT_SIGMA_K=1.8`. Offline
  `_size_spike_duration.py` confirms M-002 crosses 4.0 at ~16 samples. Committed.
- ☑ Step 3 — built `simulator:synth4` then `synth5` (carries the duration fix),
  deployed rev `ca-simulator--v2606042318`, healthz 200.
- ☑ Step 4 — verified live: M-002 fires (6.80>4.0), M-003 fires (1.96/3.90/16.53),
  M-001 unchanged (21/2h), M-004 sub-12 false positives blocked.
- ☑ Step 5 — teardown: `ca-simulator` scaled to 0 replicas; Fabric capacity
  suspended. Resume: `--min-replicas 1 --max-replicas 1` + `az fabric capacity resume`.

_Earlier section retained below._

## DONE — Injection UX overhaul: visible bands, 5 strength levels, Fabric detections (live)

## DONE — Injection UX overhaul: visible bands, 5 strength levels, Fabric detections (live)

User goal (Italian): the inject buttons did nothing visible; make them work
with 5 shared strength levels (one central selector for all machines), shade
the injection window as a band on the chart, surface the Fabric model's
detections on the panel, and flag detections with no matching injection.
Instruction: proceed to the end, commit every step, deploy fully.

- ✅ Step 1 — `control.py`: level state + `InjectionWindow`/`Detection` +
  `record_injection`/`add_detections`/`events()` (commit 5c9e79a).
- ✅ Step 2 — `simulate_machines.py`: level-scaled multi-second overlays;
  spike = sustained elevated band; record each window (commit 623336a).
- ✅ Step 3 — `server.py`: `GET /api/events` + `POST /api/level` (f30c9e7).
- ✅ Step 4 — `fabric_poller.py` + `cloud_runner` wiring + deps (6e30403).
- ✅ Step 5 — webapp data layer: types/client/useFleet (087f479).
- ✅ Steps 6-7 — webapp UI: level picker + chart bands/markers + styles
  (b467295).
- ✅ Step 8 — `deploy.ps1`: managed identity + Fabric query env (9186202).
- ✅ Step 9 — built image `synth2`, deployed revision
  `ca-simulator--v2606042100`, assigned system-assigned managed identity
  (principalId `08e2e0ce-…`), granted Viewer on `kql_telemetry` via
  `tools/_grant_kql_viewer.py` (commit 9d6b489). Verified live: `/healthz`
  200, logs show `[fabric_poller] +N detection(s)` per poll.

**Nothing pending for this feature.** Possible future polish: code-split the
776 KB webapp bundle; expose the matched/unmatched legend counts.

---

## DONE — Replace M-002 with a synthgen-simulated CNC machine (live)

User goal (Italian): swap M-002 in the live pipeline for a new synthgen CNC
machine — in the simulator, in the Fabric inference, in the Fabric dashboard,
and in the docs; wipe all existing Fabric data to restart; step by step with
progressive commits + deploys, testing at each step.

- ✅ Step 1 — `tools/build_synth_trace.py`: fit synthgen on the real CNC
  telemetry, generate a 1 Hz 24 h trace (86 400 steps, duty 99.6% vs real
  99.2%; marginals near-identical), clamp to real ranges. Artifacts:
  `_local/synthgen/synth_trace_full.npz` (gitignored) +
  `simulator-cloud/src/synth_trace_M-002.json` (4 h, 14 400 steps, committed).
- ✅ Step 2 — `SynthMachine` in `simulate_machines.py` loops the trace behind
  the same polymorphic interface as `CNCMachine`; wired through
  `build_machines`, CLI (`--synth-trace`/`--synth-machine-id`), `cloud_runner`
  (`SIM_SYNTH_PROFILE`/`SIM_SYNTH_MACHINE`), `deploy.ps1`, `Dockerfile`.
- ✅ Step 3 — `tools/train_m002_synth.py`: retrain the M-002 TransformerAE on
  the synthgen trace (3 mandrino_* features); FP16 ONNX (492 KB) fits the
  Kusto row budget, parity 3.2e-5, threshold p99.5=3.60. Overwrote
  `models/transformer_ae_small__M-002/`.
- ✅ Step 4 — wiped all Fabric data (`tools/clear_history.py`: raw_telemetry,
  anomalies, injected_anomalies → 0 rows; re-cleared after cutover to drop the
  transient FSM residue).
- ✅ Step 5 — registered the new M-002 model (`tools/05_register_model.py`,
  version 2 with mandrino_* sensors). `fn_score_demo_M002()` + update policy
  were already generic (read sensors/threshold from metadata) → no KQL change.
- ✅ Step 6 — built image `acrsimnsb7uf.azurecr.io/simulator:synth1`, deployed
  to Container App `ca-simulator` (rev `v2606042001`, healthy). Verified live
  ingest: M-002 emits only `mandrino_load/power/torque`; 22 sensors total.
- ✅ Step 7 — `tools/04_create_dashboard.py`: CNC tiles already group by
  machine_id → M-002 appears alongside M-003; redeployed dashboard
  `rtd_telemetry_live`.
- ✅ Step 8 — docs updated (architecture, data_modeling, model_architecture,
  RUNBOOK, root + simulator READMEs) + this PLAN + STATE + repo memory.
- ✅ Step 9 — final e2e verification (M-002 scored detections in `anomalies`).

## DONE — synthgen: SOTA hybrid synthetic data generator (local + Azure ML)

User goal (Italian): generate synthetic CNC spindle telemetry as faithful as
possible to the real data; method defined in local fast loops, full training on
Azure ML with the SAME code (config-only difference). Approach = hybrid:
Markov/HMM regime on `fase` + conditional 1-D diffusion (DDPM) for the 3 signals
+ histogram/point-process timing for irregular sub-second timestamps.

- ✅ `synthgen/` package (config, features scaler, data views, fidelity metrics,
  regime/timing/diffusion models, pipeline `fit`/`generate`, AML helpers).
- ✅ `configs/synthgen.yaml` (defaults + `local`/`cloud` overrides).
- ✅ `cloud-training-synth/src/train_diffusion.py` + `environment/conda.yml`
  (AML GPU entrypoint; trains + logs fidelity to MLflow; writes `outputs/`).
- ✅ `notebooks/08_synthgen_pipeline.ipynb` — 10-step instructive orchestrator:
  setup → EDA → dataset → regime/timing → diffusion smoke (local) → fidelity →
  submit AML (returns job_name) → poll/stream → download → local test + scorecard
  → export long schema. Cells 7-9 idempotent/independent.
- ✅ `.gitignore` excludes staged job-snapshot dirs under `cloud-training-synth/src/`.
- ✅ Verified end-to-end locally (fit+generate+fidelity on a 1-day subset; bundle
  staging; notebook validates, 27 cells).
- ⬜ NEXT (user-driven): run notebook cells 7-9 to submit the full GPU training,
  download the model, and review the final fidelity scorecard. Tune epochs /
  `physics_lambda` / window if marginals or correlation error are high.

## DONE — Server-side telemetry history for the charts (deployed)

User report: injected anomalies weren't visible on the dashboard, and putting
the browser in the background lost data / made the charts jump. Fix: persist a
rolling per-sensor history server-side and render the charts from it.

- ✅ `control.py`: per-machine `history` ring (`history_window_s`, default 300 s)
  appended on every `update_status` tick; `ControlState.history(since)` returns
  columnar per-sensor arrays newer than `since`.
- ✅ `server.py`: new auth-gated `GET /api/history?since=<epoch_s>` (0 = full
  window backfill; otherwise incremental).
- ✅ `webapp`: `ApiClient.getHistory(since)`, `HistoryResponse` type, and
  `useFleet` rewritten to feed charts from `/api/history` (1 Hz samples, not the
  2 s poll). Incremental via a `sinceRef` high-water mark; on (re)activation /
  return-from-hidden it backfills the whole window → no gaps/jumps. Server time
  aligned to the local clock via an offset.
- ✅ Redeployed: image `simulator:web6` (build `nfj`), revision
  `ca-simulator--v2606031733` (100% traffic, healthy); `/api/history` → 401
  (route wired, gated).

## DONE — Control-panel rewrite to React + Recharts (deployed)

User request (Italian): separate per-sensor charts (fixed height each → taller
column when a machine has more sensors), numbered X/Y axes, zero always visible,
use a real charting library + best practices (React preferred, not plain
vanilla), and fix the light/dark theme bug. Complete and redeployed.

- ✅ `webapp/` rewritten as a Vite + React 19 + TypeScript SPA (Recharts +
  @azure/msal-browser), bundled locally (CSP `script-src 'self'`, no CDN).
- ✅ One fixed-height (132px) Recharts chart per sensor, stacked vertically;
  numeric time X-axis (HH:MM:SS, 5-min window) + numeric Y-axis with domain
  `[min(0,lo), max(0,hi)]` + 8% headroom → zero always visible.
- ✅ Theme bug fixed: React `ThemeProvider` drives CSS vars **and** feeds
  explicit palette colors into Recharts, so charts recolor on light/dark toggle.
- ✅ Multi-stage `Dockerfile` (node build → python runtime); `deploy.ps1`
  staging excludes `node_modules`/`dist`; `server.py` `/config.js` →
  `window.CONFIG`. Old vanilla `app.js`/`styles.css`/`vendor/` deleted.
- ✅ `npm run build` passes. Redeployed: ACR image `simulator:web5` (build
  `nfh`) → `ca-simulator` revision `ca-simulator--v2606031616` active + healthy.

## DONE — Simulator control-panel improvements (deployed)

User request (Italian): improve the simulated-machines dashboard. Code complete
(py_compile OK + CNC forced-mode functionally tested) and redeployed.

- ✅ Light/dark theme toggle — `webapp/styles.css` (`--accent-text`/`--track` +
  `:root[data-theme="light"]`), `webapp/index.html` (`#theme-btn`), `webapp/app.js`
  (`initTheme`/`applyTheme`/`toggleTheme`, localStorage `panel-theme`).
- ✅ Scrolling fixed-window chart X axis — `drawChart()` right edge = now,
  5-min window mapped to width; few points sit at the right and scroll left.
- ✅ Zero-based chart Y axis — baseline at 0, top auto-scales.
- ✅ M-003 forced state — `cnc_engine.py` `forced_mode`/`set_forced_mode` +
  `step()`; `CNCMachine.valid_states=["ACTIVE","IDLE"]` + `set_forced_state`
  (both `simulator-local/` and `simulator-cloud/` copies).
- ✅ Redeployed cloud simulator: ACR image `simulator:web4` (build `nfg`) →
  `ca-simulator` revision `ca-simulator--v2606031550` active + healthy.

## DONE — Per-machine model-quality metrics on the Fabric dashboard

User request (Italian): every machine on the dashboard must show model-quality
metrics (P/R/F1). We can compute them because the simulator can emit the
injected-anomaly type. Fix the simulator for M-003/M-004 (note: marker emission
was actually missing on ALL machines, not just 3/4).

Code complete (compiles) and DEPLOYED to Fabric (user-confirmed):
- ✅ `tools/eval_offline.py` (new) — offline supervised eval; labeled synthetic
  data + ONNX scoring + numpy metrics; results in `data/eval/offline_metrics.json`.
- ✅ `simulator-local/` + `simulator-cloud/` — emit `__inject__<kind>:<sensor>`
  markers (value=duration_s, quality=-1) on every overlay start, all machines.
- ✅ `kql/04_update_policy.kql` — `fn_score_demo_M004()` + 4th update-policy entry.
- ✅ `kql/07_classification.kql` — `fn_correlation_metrics_by_machine()` (one row
  per machine).
- ✅ `tools/04_create_dashboard.py` — `Q_METRICS_BY_MACHINE` + "Model quality by
  machine" table tile; subsequent tiles shifted down +6.

Deployed (2026-06-02, user-confirmed "confermo"):
- ✅ Registered M-004 model: `tools/05_register_model.py models/transformer_ae_small__M-004/` (Kusto `models` v1, fp16).
- ✅ Redeployed KQL 04/07 via `tools/02_setup_kql_tables.py`.
- ✅ Pushed the new dashboard tile via `tools/04_create_dashboard.py`.
- ✅ Redeployed cloud simulator: ACR `simulator:web3` → `ca-simulator` revision
  `v2606021618` (Single, healthy, machine_count=4) — markers now live for all machines.
- ✅ Committed (Conventional Commits) + pushed.

## DONE — Forced machine state + client-side 5-min live chart

User requests (Italian):
1. OFF/IDLE are not random glitches — they are normal FSM operating-cycle
   states. Make the state **operator-controllable** (force a state, with an
   "Auto" option that returns to the FSM).
2. A **5-minute live chart** of machine values on the panel, **without
   excessive load** — must stop when the browser closes and be pausable
   (standby).

Design decision: the chart is built **entirely client-side** from the data
already returned by the existing `/api/state` poll (which already includes
`last_sample` per machine). No new server→browser stream → zero extra backend
load; it stops on page close (polling stops) and is pausable. Also auto-pauses
when the browser tab is hidden.

Changes (code complete, compiles; control.py logic unit-tested):
- ✅ `simulator-cloud/src/control.py` — `_MachineEntry.forced_state` +
  `valid_states`; `set_forced_state()` (validates / None=auto), loop-side
  `forced_state()`; both exposed in `snapshot()`.
- ✅ `simulator-cloud/src/simulate_machines.py` — `Machine.forced_state`
  field + `set_forced_state()` + `valid_states` (pins FSM state in `step()`);
  `CNCMachine` no-op + empty `valid_states`; `run()` applies the override
  each tick before `m.step()`.
- ✅ `simulator-cloud/src/server.py` — `StateBody` + `POST
  /api/machines/{id}/state` (422 on bad state, 404 unknown machine).
- ✅ `webapp/` — per-card "Force state" `<select>` (hidden for CNC M-003);
  header **Charts** toggle + **Pause** standby button; per-card `<canvas>`
  rolling 5-min chart fed from the poll; auto-pause on tab hidden.
  Files: `index.html`, `app.js`, `styles.css`.

Pending:
- ✅ Built image `simulator:web2` (ACR run `nfc`), redeployed `ca-simulator`
  revision `0000005`, verified live (healthz/panel/app.js assets +
  `/api/machines/{id}/state` 401 gated).
- [ ] Commit (Conventional Commits) + push.

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

7. ✅ **Live deploy (user authorized).** Rebuilt+pushed image
   `simulator:v2606011853`, enabled external ingress (port 8080) + control
   env/secret on `ca-simulator` (revision `0000003`), deployed SWA
   `swa-anomaly-sim`, published `webapp/` via SWA CLI. Validated live:
   healthz/state/toggle/inject + 401/422/404 + CORS all pass.
   - Panel URL: `https://jolly-pebble-0d6f26703.7.azurestaticapps.net`
   - Control FQDN: `https://ca-simulator.thankfulground-943b41a0.italynorth.azurecontainerapps.io`
   - Demo API key in git-ignored `_local/_control_api_key.txt`.

**Cost shutdown is the user's responsibility** (per instruction): stop the
container app + pause Fabric capacity to minimize cost. When the container is
stopped the panel shows its offline banner and stays inactive — exactly the
requested behavior.

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
