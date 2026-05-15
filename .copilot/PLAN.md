# Active plan — Simulator + training redesign

_Last updated: 2026-05-15_

## Goal

Make the demo physically credible and able to handle realistic operating
conditions: multiple valid load regimes, valid transients, machines that
turn off and back on. Make the training pipeline aware of all of this.

## Phases

### Phase 1 — Physics-based simulator
- [ ] Replace independent per-sensor generators with a single machine
      state model that produces all 8 sensors coherently
      (load → current/RPM/power/vibration/temperature couplings, with
      first-order thermal dynamics).
- [ ] Add a probabilistic FSM with states: `OFF`, `STARTUP`, `IDLE`,
      `PRODUCTION_LIGHT`, `PRODUCTION_HEAVY`, `RAMP_UP`, `RAMP_DOWN`,
      `SHUTDOWN`. Transitions between load regimes always go through a
      `RAMP_*` state.
- [ ] During `OFF`, the simulator stops publishing (real gap) — does
      **not** publish zeros. (See open question #2.)
- [ ] Keep the event payload identical
      (`machineId, sensorId, ts, value, quality`).
- [ ] Add CLI flags: `--off-prob`, `--regime-mix`, `--shift-pattern`.

### Phase 2 — Off/on awareness in the KQL pipeline
- [ ] Decide design: option **A** (new `machine_state` table updated by
      a policy on `raw_telemetry`), option **B** (filter only at training
      time), option **C** (add `is_running` to wide MV). Current lean: A+B.
- [ ] Update `scripts/deploy.ps1` if new tables/policies are needed.
- [ ] Update Activator rules to ignore alerts when `state != RUNNING` or
      `ts < startup_time + grace_seconds`.

### Phase 3 — Training data selection + transient augmentation
- [ ] Segment the timeline into continuous `RUNNING` runs; drop runs
      shorter than `min_run_length` (e.g. 5 min).
- [ ] Tag each window as `STEADY` or `TRANSIENT` (e.g. via |Δload|
      threshold or via the simulator-published state, see question #3).
- [ ] Sliding window with adaptive stride: `STEADY` stride 16,
      `TRANSIENT` stride 1.
- [ ] Augment transient windows: time warping ±20%, magnitude scaling
      ±10%, controlled jitter, until ratio reaches ~30/70 transient/steady.
- [ ] Stratify train/val split by whole segments (no leakage across
      window boundaries).

### Phase 4 — Model architecture
- [ ] Pick architecture (see open question #4). Current recommendation:
      **Conv1D + GRU autoencoder** (small, ONNX-friendly, runs on CPU
      for KQL inference). Stretch goal: **VAE** if multimodality matters.
- [ ] Loss: MSE on normalized features + L1 on time-step deltas.
- [ ] Threshold: per-regime (cluster latents) or 99.5th percentile on
      cleaned training set, instead of a single global μ + K·σ.
- [ ] Re-export ONNX with the new architecture; update KQL scorer.

## Open questions (need user input before coding)

1. **FSM granularity**: Markov FSM with 5–7 states, OR deterministic
   "recipe" per machine (e.g. every 15 min run a fixed cycle)?
2. **OFF representation**: silence (no events) OR `quality=0` events?
3. **Regime ground truth**: should the simulator publish a `state`
   channel that the training pipeline can use, or must everything be
   inferred from sensors only?
4. **GPU**: is the tunnel-connected GPU PC available for training? If
   yes, Transformer/VAE become viable; if not, stay with Conv+GRU.
5. **Schema compatibility**: can existing KQL tables (`raw_telemetry`,
   wide MV) be modified, or must changes be additive?
6. **Rollout**: one big PR, or incremental commits (Phase 1 → 2 → 3 → 4)?

## How to use this file

- Tick boxes as items complete.
- When a phase finishes, move its checklist to a `## Done` section at
  the bottom (or delete it if everything is reflected in `STATE.md`).
- Add new phases at the top of "Phases" only after agreement with the
  user.
