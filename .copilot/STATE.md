# Current state

_Last updated: 2026-05-21 (handoff from CPU laptop → GPU PC)_

## Where we are

**Demo end-to-end is live.** Cloud simulator → Eventstream → KQL
(`raw_telemetry`) → update policies (`telemetry_wide_mv`, `anomaly_scores`,
`injected_anomalies`) → real-time dashboard with TP/FP/FN tiles.

Latest commit pushed: `a15345a` on `main`
("feat: add ground-truth correlation + FP/FN classification pipeline").

### Fabric environment (do NOT modify without confirmation)
- Workspace `anomaly-detection-fresh` (id `35627f40-dcb7-4346-b867-1b04603a8094`),
  capacity F4, RG `fabric-anomaly-detection`, region `italynorth`.
- KQL DB `kql_telemetry` (id `142c5513-05ab-4762-8e9a-3fe60bd5bf3c`),
  cluster `https://trd-53389re9vz38nbzpgn.z5.kusto.fabric.microsoft.com`.
- Container App `ca-simulator` revision `ca-simulator--0000001`.
- Dashboard `rtd_telemetry_live` (id `3dc83f28-04ed-4cd4-b77d-5c98c7ade918`),
  11 tiles incl. Precision/Recall/F1 + TP-vs-FP timeline.

### Current model
`models/transformer_ae_small/`:
- 161 984 params (window=64, 8 features, d_model=56, 4 heads, 2 enc + 2 dec,
  ff_dim=160). Trained on **CUDA**, 12 epochs, batch 256, lr 3e-4.
- Threshold 0.0154 (p99.5 of training-val window scores).
- Detection-delay medians on eval set: bearing 578 s, hydraulic 1263 s,
  sensor_stuck 664 s.

### Latest live metrics (2h lookback, grace 2m)
- TP_det=34, FP=0, TP_inj=50, FN=233.
- **Precision 100%, Recall 17.7%, F1 30.1%**.
- median latency 68.6 s, P90 109.97 s.
- **Real issue: recall is low** on drift/stuck for M-002..M-005. Not FPs.

## Outstanding housekeeping

1. **`SIM_ANOMALY_PROB` is bumped to `0.01`** on the cloud simulator
   (was 0.0005) for faster verification. **Restore** before declaring
   demo-ready:
   ```powershell
   az containerapp update -g fabric-anomaly-detection -n ca-simulator `
       --set-env-vars SIM_ANOMALY_PROB=0.0005
   ```

## Active focus → on the GPU PC

Goal: raise recall from ~18% to ≥60% without losing precision.

See `.copilot/PLAN.md` for the full retraining checklist. TL;DR:

1. `git pull` in this repo on the GPU PC.
2. Recreate `.venv` (CUDA-enabled torch this time): see PLAN.
3. Regenerate datasets via `notebooks/01_simulator_dev.ipynb`
   (training §7.1, eval §8). ~10 min CPU, no GPU benefit there.
4. Open `notebooks/06_train_transformer_small.ipynb`. Already has
   `device = torch.device("cuda" if torch.cuda.is_available() else "cpu")`.
   Try the 3 changes below in order — first one that pushes recall ≥40%
   wins; otherwise stack them.
   - **(A) Lower threshold** in `kql/04_update_policy.kql` from p99.5
     (0.0154) to p99 (~0.010) or p98 (~0.008). No retrain needed —
     just push the new constant via `tools/02_setup_kql_tables.py`.
   - **(B) Longer window** (`WIN=128` or `256`) — drift/stuck are slow
     and 64 s is too short. Retrain.
   - **(C) Per-sensor scoring** — change anomaly score to
     `max_over_features(MSE_per_feature)` instead of mean. Retrain.
5. Re-register the model with `tools/05_register_model.py` and
   re-run `python tools/06_correlate.py --lookback 2h --grace 2m`.

## Resume protocol on the GPU PC

```powershell
cd <repo-root>
git pull
# new venv on Windows with CUDA torch
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install torch --index-url https://download.pytorch.org/whl/cu121
pip install -r tools/requirements-sim.txt -r simulator-local/requirements.txt
.\.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# .env is gitignored: copy it manually from this PC (or recreate from .env.example)
```

Then continue with step 3 above.

## Recent context the user might mention
- The user works across two PCs via VS Code Remote Tunnels; chat history
  doesn't sync — that's why this folder exists.
- Auth helper: `tools/_fabric_auth.get_credential(tenant, scope, repo_root)`.
  Each script defines `SCOPE = "https://api.fabric.microsoft.com/.default"`
  locally (do NOT import `FABRIC_SCOPE` from `_fabric_auth`).
- KQL gotchas already documented in `kql/` files: `kind` is reserved
  (use `anomaly_kind`); adjacent `.alter` commands need blank lines;
  no inline `//` comments inside `.create-merge table (...)` column lists.
