# Current state

_Last updated: 2026-06-01 (M-003 real-data CNC machine added, 3 machines live)_

## Where we are

**Production-realistic 3-machine + 3-model architecture is live.** Cloud
simulator → Eventstream → KQL (`raw_telemetry`) → per-machine update policies
(`fn_score_demo_M001`, `fn_score_demo_M002`, `fn_score_demo_M003`) →
`anomalies` → real-time dashboard. One ONNX model per machine, each with its
own scaler and threshold read from the model metadata.

**M-003 is a real-data CNC spindle machine** (3 sensors: `mandrino_load` %,
`mandrino_power` kW, `mandrino_torque` N*cm) whose normal behaviour is
driven by a recorded CNC profile (`data/cnc_profile_M-003.json`, derived
from `_data_local/`). M-001/M-002 remain the synthetic 8-sensor machines.
Validated end-to-end on 2026-06-01: M-003 ingests (3 sensors), scores
~1.17 on normal data (threshold 1.882), and an injected drift produced an
anomaly at score 9.79 (~5.2× threshold) landing in the `anomalies` table.

### Why we pivoted (was: "retrain bigger model")
The previous PLAN diagnosed the 17.7% recall as a window/model capacity
issue. Real cause: the scoring function `fn_score_demo()` was hardcoded
to `machine='M-001'` (in `kql/04_update_policy.kql` line 28). All true
positives were on M-001; all M-002..M-005 anomalies were false negatives
purely because they were never scored. Confirmed via
`python tools/06_correlate.py --lookback 2h`.

Rather than re-hardcode for 5 machines, we cut the fleet to a
production-realistic shape: 2 machines + 1 dedicated model each.

### Fabric environment (do NOT modify without confirmation)
- Workspace `anomaly-detection-fresh` (id `35627f40-dcb7-4346-b867-1b04603a8094`),
  capacity F4 `anomalydetectiondemo`, RG `fabric-anomaly-detection`,
  region `italynorth`.
- KQL DB `kql_telemetry` (id `142c5513-05ab-4762-8e9a-3fe60bd5bf3c`),
  cluster `https://trd-53389re9vz38nbzpgn.z5.kusto.fabric.microsoft.com`.
- Eventstream EH endpoint (the working one used by both legacy and new sim):
  `esehitnfrrdlj1y644v1isl.servicebus.windows.net` /
  `esehitnfrrdlj1y644v1isl_eh`. This is the value in `.env` ->
  `EVENTSTREAM_CONNECTION_STRING`.
- Dashboard `rtd_telemetry_live` (id `3dc83f28-04ed-4cd4-b77d-5c98c7ade918`).

### Container Apps (cloud simulator)
Two apps existed at start; only ONE is active now.

| RG | App | Revision | State | Notes |
|---|---|---|---|---|
| `fabric-anomaly-detection` | `ca-simulator` | `ca-simulator--0000002` | **deactivated** | legacy 5-machine sim, deactivated 2026-05-21 |
| `rg-fabric-demo` | `ca-simulator` | `ca-simulator--v2606011249` | Running | 3-machine sim (M-001/M-002 synthetic + M-003 CNC), image `acrsimnsb7uf.azurecr.io/simulator:latest` |

Env on the active app: `SIM_MACHINES=3`, `SIM_RATE=1.0`,
`SIM_ANOMALY_PROB=0.0005` (restored to demo rate),
`SIM_CNC_PROFILE=/app/cnc_profile_M-003.json` (drives M-003 from the real
CNC profile baked into the image).

### Models
Two artifacts in `models/`, each trained from
`data/training/telemetry_wide.parquet` filtered to one machine
(via `tools/train_per_machine.py`, GPU, 12 epochs, ~25 s each):

| Dir | Model name | Machine | Sensors | Threshold (p99.5 val) |
|---|---|---|---|---|
| `models/transformer_ae_small__M-001/` | `transformer_ae_small__M-001` | M-001 | 8 (synthetic) | 1.00679 |
| `models/transformer_ae_small__M-002/` | `transformer_ae_small__M-002` | M-002 | 8 (synthetic) | 0.98171 |
| `models/transformer_ae_small__M-003/` | `transformer_ae_small__M-003` | M-003 | 3 (real CNC) | 1.88170 |

M-003 (`n_parameters=161 419`, 3 input features) was trained on telemetry
reconstructed from the real CNC profile in `data/cnc_profile_M-003.json`
(provenance: `_data_local/`, git-ignored). Registered to the live `models`
table at version 1 (FP16 ONNX, 491.9 KB raw / fits the Kusto 1 MB budget).

(Old `models/transformer_ae_small/` is left in place but is no longer
wired into any KQL update policy. Its threshold 0.0154 is not comparable
because it was trained on all 10 machines with a single combined scaler;
the new per-machine scalers are tighter and produce scores ~65x larger.)

Architecture (identical for both): TransformerAE, WINDOW=64, D_MODEL=56,
4 heads, 2 enc + 2 dec, FF_DIM=160, ~161 984 params. Score function
baked into ONNX: `per_sensor.max(dim=1)` of MSE mean-over-time-per-feature.
Both `model.fp16.onnx` are 493 KB raw / 657 KB base64 (fits Kusto's
1 MB row budget).

### KQL pipeline
- `kql/04_update_policy.kql` — REPLACED. Defines three per-machine
  scoring functions:
  - `fn_score_demo_M001()` calls `score_multivariate_onnx_batch(
    model_name='transformer_ae_small__M-001', machine='M-001', bin=1s,
    threshold=<from metadata>)`.
  - `fn_score_demo_M002()` analogous for M-002.
  - `fn_score_demo_M003()` analogous for M-003 (3-sensor CNC model).
  All three are attached to the `anomalies` update policy. The file also
  drops the legacy `fn_score_demo` and `fn_score_multivariate_demo`
  functions (`ifexists`).
- `kql/05_multivariate_mv.kql` — the materialized view
  `raw_telemetry_wide_mv` and the helper functions are kept (useful
  for ad-hoc queries), but `fn_score_multivariate_demo()` and its
  update-policy attach are REMOVED. The MV is no longer wired into
  the live scoring path.
- `kql/03_scoring_functions.kql` — unchanged; `score_multivariate_onnx_batch`
  already reads the scaler from `metadata.scaler` and normalises sensors
  before invoking ONNX (this matches `tools/train_per_machine.py`).

### Tools changed / added
- `tools/train_per_machine.py` — NEW. Trains one model per machine from
  the existing training parquet (no data regen needed).
- `tools/purge_obsolete_machines.py` — NEW. Tried to `.purge` rows for
  M-003..M-005; **failed with 403 Forbidden** (operation
  `PurgeTableRecordsCommand` not allowed for our Entra principal in this
  Fabric Eventhouse). Skipped: old rows age out with the 30-day
  softdelete retention and live correlation uses a short lookback.
- `simulator-cloud/deploy.ps1`, `simulator-cloud/src/simulate_machines.py`,
  `simulator-cloud/src/cloud_runner.py`,
  `simulator-local/simulate_machines.py` — defaults changed from
  5 machines to 2, then extended for M-003 (3rd machine, real CNC).
  `deploy.ps1` now takes `-CncProfile` (default
  `/app/cnc_profile_M-003.json`), injects `SIM_CNC_PROFILE`, and skips the
  Fabric-capacity RG/region lookup when both `-RgName` and `-Location` are
  passed (the simulator lives in a different subscription than the Fabric
  capacity, so the lookup would otherwise fail).
- `tools/inject_anomaly.py` — used to validate M-003 end-to-end by
  injecting aligned high-value slices on all 3 CNC sensors
  (`mandrino_load/power/torque`); the multivariate window needs all
  sensors present, so single-sensor injection is not enough.

### This session (2026-06-01) — M-003 wiring + the real reason ingestion stalled
- **Root cause of “no data since May 27”:** the eventstream destination
  `kql_raw_telemetry` (id `2ba1d00a-c072-4391-9439-06b49d213864`) on
  eventstream `es_machines` (id `b55b0263-0916-4366-9931-a7da43bd4a47`) was
  **Paused** — NOT a simulator or model problem.
  `tools/_check_topology.py` auto-resumes any Paused destination
  (`POST /eventstreams/{id}/topology/destinations/{id}/resume` with
  `{"startType":"Now"}`, HTTP 200). After ~75 s batching, all 3 machines
  resumed ingesting.
- M-003 model registered to live `models` table (version 1) via
  `tools/05_register_model.py models/transformer_ae_small__M-003`.
- `kql/04_update_policy.kql` applied live via
  `tools/02_setup_kql_tables.py kql/04_update_policy.kql` (6 commands OK).

### Cloud training on Azure ML (NEW, 2026-06-01)

The per-machine models can now be (re)trained **entirely in the cloud** on
Azure ML — no local GPU and no training parquet required (synthetic 8-sensor
telemetry is generated from the repo simulator physics inside the job).

- AML stack deployed via `infra/ml-workspace.bicep` to **westeurope**, RG
  `rg-anomaly-ml-westeurope`: workspace `anomalyml-mlw`, storage
  `stanomalymlfknlnf4v`, KV, App Insights, Log Analytics, compute cluster
  `cpu-cluster` (`Standard_DS3_v2`, min=0 max=1, scale-to-zero).
- Code: `cloud-training/generate_and_train.py` (self-contained: sim physics
  + train + FP16 ONNX export), `cloud-training/conda.yml`,
  `cloud-training/submit_job.py` (azure-ai-ml SDK: submit + poll + AAD
  artifact download). `cloud-training/job.yml` is reference only.
- Run it: `.\.venv\Scripts\python.exe cloud-training/submit_job.py`
  → trains M-001 + M-002, downloads into `models/transformer_ae_small__<M>/`.
- Last run `loyal_juice_mps6ccsph1` (Completed): M-001 val 0.158 / thr 1.475,
  M-002 val 0.147 / thr 1.029; both FP16 ONNX Kusto-fit, parity rel diff
  <0.2%. NOTE: thresholds differ from the local-trained values above because
  the cloud job uses freshly generated synthetic data.

**Gotchas captured (see also `/memories/session/azure-ml-training-plan.md`):**
- Host is Windows **ARM64** → `az extension add -n ml` hangs (no win-arm64
  wheels for native deps). Use the **azure-ai-ml Python SDK**, not the CLI ext.
- Storage has `allowSharedKeyAccess=false` → SDK `jobs.download` (key auth)
  fails. Download artifacts/logs via **AAD** (`BlobServiceClient` +
  `AzureCliCredential`); the signed-in user needs **Storage Blob Data
  Contributor** on the storage account.
- conda pins `torch==2.3.1` → `torch.onnx.export(dynamo=...)` (torch ≥2.5)
  is NOT supported; do not pass `dynamo=`.
- **All GPU AML quota is 0** in westeurope/italynorth/swedencentral/northeurope
  (separate BatchAI "Cluster Dedicated" quota). Min GPU to request:
  `Standard_NC4as_T4_v3`, quota "Standard NCASv3_T4 Family Cluster Dedicated
  vCPUs" ≥ 4 in westeurope. The model is tiny so CPU training is ~2 s/epoch.
- Old `rg-anomaly-ml-italynorth` RG is unusable (no per-family AML quota) —
  safe to delete.

## Outstanding housekeeping

1. **Validate live correlation** with the new pipeline. Run after at
   least 15 min of fresh ingestion:
   ```powershell
   .\.venv\Scripts\python.exe tools\06_correlate.py --lookback 30m --grace 2m
   ```
   Target: Precision >= 95% and Recall >= 60% on M-001, M-002 and M-003.
   (At `SIM_ANOMALY_PROB=0.0005` natural anomalies are rare, so a short
   window may show few/no labelled events; lengthen the lookback or
   temporarily raise the rate if you need a populated correlation.)

2. **Demo anomaly rate is set to 0.0005** (restored this session). No
   action needed unless you bump it for a faster validation run.

3. **Optional cleanup** — the legacy container app
   `fabric-anomaly-detection/ca-simulator` has all revisions deactivated
   and `min-replicas=0`. Safe to delete the whole app + its ACR
   `acrsim3l8kge` if you want to tidy up subscription cost.

4. **`.env` was updated** with the working EH connection string
   (`esehitnfrrdlj1y644v1isl_eh`); the previous value
   (`esehitnfw9yg51rnseq802n_eh`) was stale and DNS-unresolvable. Keep
   the new value; the new simulator app's secret is also synced.

## Resume protocol

```powershell
cd <repo-root>
git pull
.\.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
.\.venv\Scripts\python.exe tools\06_correlate.py --lookback 30m --grace 2m
```

## Conventions to remember
- The user works across two PCs via VS Code Remote Tunnels; chat history
  doesn't sync — that's why this folder exists.
- Auth helper: `tools/_fabric_auth.get_credential(tenant, scope, repo_root)`.
  Each script defines `SCOPE = "https://api.fabric.microsoft.com/.default"`
  locally.
- `.\.venv\Scripts\Activate.ps1` does NOT actually swap `python` in pwsh
  terminals on this PC. Always invoke the venv interpreter explicitly:
  `.\.venv\Scripts\python.exe ...`.
- KQL gotchas: `kind` is reserved (use `anomaly_kind`); `last` is reserved
  (use `last_at`); adjacent
  `.alter` commands need blank lines; no inline `//` comments inside
  `.create-merge table (...)` column lists; `tools/02_setup_kql_tables.py`
  splits commands on blank lines only — keep them between `.drop`s.
- **`az acr build` on Windows ARM64 crashes client-side** with
  `UnicodeEncodeError: 'charmap' ... cp1252` in colorama log streaming
  (chcp 65001 / no_color / PYTHONUTF8 do NOT fix it). The build still
  **succeeds server-side** — verify with
  `az acr task list-runs -r acrsimnsb7uf --top 5 -o table`, then update the
  Container App directly with `az containerapp update` (no log streaming).
