# Current state

_Last updated: 2026-05-21 (per-machine architecture rollout)_

## Where we are

**Production-realistic 2-machine + 2-model architecture is live.** Cloud
simulator → Eventstream → KQL (`raw_telemetry`) → per-machine update policies
(`fn_score_demo_M001`, `fn_score_demo_M002`) → `anomalies` → real-time
dashboard. One ONNX model per machine, each with its own scaler and
threshold read from the model metadata.

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
| `rg-fabric-demo` | `ca-simulator` | `ca-simulator--0000001` | Running | new 2-machine sim, image `acrsimnsb7uf.azurecr.io/simulator:latest` |

Env on the active app: `SIM_MACHINES=2`, `SIM_RATE=1`, `SIM_ANOMALY_PROB=0.01`
(temporarily, for validation; restore to `0.0005` before declaring
demo-ready — see Outstanding).

### Models
Two artifacts in `models/`, each trained from
`data/training/telemetry_wide.parquet` filtered to one machine
(via `tools/train_per_machine.py`, GPU, 12 epochs, ~25 s each):

| Dir | Model name | Machine | Threshold (p99.5 val) |
|---|---|---|---|
| `models/transformer_ae_small__M-001/` | `transformer_ae_small__M-001` | M-001 | 1.00679 |
| `models/transformer_ae_small__M-002/` | `transformer_ae_small__M-002` | M-002 | 0.98171 |

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
- `kql/04_update_policy.kql` — REPLACED. Defines two per-machine
  scoring functions:
  - `fn_score_demo_M001()` calls `score_multivariate_onnx_batch(
    model_name='transformer_ae_small__M-001', machine='M-001', bin=1s,
    threshold=<from metadata>)`.
  - `fn_score_demo_M002()` analogous for M-002.
  Both are attached to the `anomalies` update policy. The file also
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
  5 machines to 2.

## Outstanding housekeeping

1. **Validate live correlation** with the new pipeline. Run after at
   least 15 min of fresh ingestion:
   ```powershell
   .\.venv\Scripts\python.exe tools\06_correlate.py --lookback 30m --grace 2m
   ```
   Target: Precision >= 95% and Recall >= 60% on BOTH M-001 and M-002.

2. **Restore demo anomaly rate** once validation passes:
   ```powershell
   az containerapp update -g rg-fabric-demo -n ca-simulator `
       --set-env-vars SIM_ANOMALY_PROB=0.0005
   ```
   (Currently set to 0.01 for faster validation.)

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
- KQL gotchas: `kind` is reserved (use `anomaly_kind`); adjacent
  `.alter` commands need blank lines; no inline `//` comments inside
  `.create-merge table (...)` column lists; `tools/02_setup_kql_tables.py`
  splits commands on blank lines only — keep them between `.drop`s.
