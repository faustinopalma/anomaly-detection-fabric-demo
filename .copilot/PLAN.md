# Plan

_Last updated: 2026-05-21_

## Phase 1 — Validate the per-machine pipeline (NOW)

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
