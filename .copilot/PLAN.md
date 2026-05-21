# Active plan — Improve model recall (run on GPU PC)

_Last updated: 2026-05-21_

## Goal
Raise live recall from **17.7% → ≥60%** while keeping Precision ≥ 95%.
Median detection latency ≤ 90 s.

Diagnosis from `tools/06_correlate.py --lookback 2h --grace 2m`:
- Precision 100% (FP=0). No false alarms — threshold is not too low.
- Recall is low specifically on **drift** and **sensor_stuck** for
  machines M-002..M-005. Bearing on M-001 is detected reliably.
- Hypothesis: window=64 s is too short for slow overlays (drift 8–20 s
  ramps + stuck for 5–20 s) → score gets diluted by 50+ s of normal data.

## Phase 0 — GPU PC setup

- [ ] `git pull origin main` (commit `a15345a` or later).
- [ ] Recreate `.venv` with CUDA torch (see `.copilot/STATE.md`).
- [ ] Copy `.env` from the laptop (or recreate from `.env.example` —
      same values; do not change Fabric IDs).
- [ ] Verify `torch.cuda.is_available() == True`.
- [ ] Login: `az login --tenant <tenant>` and `fab auth login`.

## Phase 1 — Quick win: threshold sweep (no retrain)

- [ ] Compute new candidate thresholds offline in
      `notebooks/06_train_transformer_small.ipynb` (final eval cell):
      try p99 (~0.010) and p98 (~0.008).
- [ ] For each candidate, edit `kql/04_update_policy.kql` to update the
      `THRESHOLD` constant. Push via `tools/02_setup_kql_tables.py`.
- [ ] Wait 5 min for new ingest, then run
      `python tools/06_correlate.py --lookback 1h --grace 2m`.
- [ ] Pick the lowest threshold where Precision still ≥ 95%.
- [ ] If recall still < 40%, proceed to Phase 2.

## Phase 2 — Longer window + retrain

- [ ] Regenerate datasets via `notebooks/01_simulator_dev.ipynb`
      sections 7.1 (training, 10×5d) and 8 (eval, 8 machines).
      ~10 min on any CPU. Outputs:
      `data/training/telemetry_wide.parquet`,
      `data/eval/telemetry_wide.parquet`,
      `data/eval/anomaly_labels.parquet`.
- [ ] In `notebooks/06_train_transformer_small.ipynb`:
      - [ ] Set `WIN = 128` (was 64). Keep `STRIDE = 8` or set to 16 to
            keep training-set size manageable.
      - [ ] Re-fit `StandardScaler` on the new wide table.
      - [ ] Train (12 epochs, batch 256, lr 3e-4). On GPU expect
            ~5–10 min.
      - [ ] Verify val loss < 0.008 and PR-AUC ≥ 0.65 on eval set.
- [ ] Export ONNX with the new `WIN`. Save under
      `models/transformer_ae_w128/` to keep the old one for rollback.
- [ ] Update `kql/04_update_policy.kql` so `WIN = 128` and the new
      threshold are used. Push via `tools/02_setup_kql_tables.py`.
- [ ] Run `tools/05_register_model.py` to upload the new ONNX into
      Fabric and switch the active model row.
- [ ] Re-run `tools/06_correlate.py --lookback 1h --grace 2m`. Target:
      Recall ≥ 60%, Precision ≥ 95%, median latency ≤ 90 s.

## Phase 3 — Per-sensor scoring (only if Phase 2 still falls short)

- [ ] In the model's score function, replace
      `score = mean((x - x_hat) ** 2)` with
      `score = max(mean_over_time((x - x_hat) ** 2, axis=time))`
      (i.e. max across the 8 features instead of mean). This makes a
      drift on a single sensor not get drowned out.
- [ ] Recompute threshold (p99.5 on the new score distribution).
- [ ] Re-export ONNX, push, re-register, re-validate.

## Phase 4 — Demo cleanup (do last)

- [ ] Restore `SIM_ANOMALY_PROB=0.0005` on `ca-simulator`.
- [ ] Wait 1 hour, re-run correlation to confirm metrics under realistic
      injection density.
- [ ] Update `.copilot/STATE.md` with final metrics.
- [ ] Tag the repo (`v1.0-demo`).

## Done (carry-over from earlier sessions)

- ✅ Fabric environment bootstrap (workspace, eventhouse, KQL DB,
  eventstream, dashboard).
- ✅ Cloud simulator with FSM + load coupling + injection markers.
- ✅ End-to-end real-time scoring pipeline (raw → wide → scores).
- ✅ Ground-truth correlation (`kql/05`, `06`, `07`,
  `tools/06_correlate.py`).
- ✅ FP/FN classification + Precision/Recall/F1 metrics + dashboard
  tiles (commit `a15345a`).
