# Model architecture options for anomaly detection

> **Status (current):** the live demo converged on the small **TransformerAE**
> (`transformer_ae_small`, WINDOW=64), trained **per machine** — one model,
> scaler and threshold each — for the 3 live machines (M-001/M-002 synthetic
> 8-sensor, M-003 CNC 3-sensor). The options below are the design menu that
> led to that choice; the dataset notes (`M-001..M-005`, 5 machines) describe
> the original offline training set, not the current live fleet.
>
> Companion to [`.copilot/PLAN.md`](../.copilot/PLAN.md) — Phase 4.
> Goal: pick the model we'll train on `data/training/telemetry_wide.parquet`
> (clean) and evaluate against `data/eval/telemetry_wide.parquet` +
> `data/eval/anomaly_labels.parquet` (with 12 injected fault episodes
> across `bearing`, `hydraulic_leak`, `sensor_stuck`).

## 0. What every option has in common

All candidates here are **reconstruction-based unsupervised** detectors:

1. Train on **clean data only** (`data/training`, machines `M-001..M-005`).
2. Model learns to reproduce a short window of multivariate sensor data
   (e.g. 64 timesteps × 8 sensors).
3. At inference, the **reconstruction error** of a window is the
   *anomaly score*. High error ⇒ the window doesn't look like anything
   the model saw during training.
4. A **threshold** turns the score into `is_anomaly ∈ {0,1}`.
5. ONNX export so the same model can run:
   - In KQL via `python(...)` plugin (CPU, single window per call),
   - In a Spark batch / Notebook for offline backfill,
   - On the local PC for development.

What changes between options is **how** the encoder/decoder are built,
which gives different tradeoffs on:

- **Capacity** (can it learn long-range dynamics? multimodal regimes?)
- **Training cost** (CPU minutes vs GPU hours)
- **Inference cost** (matters for KQL `python()` per-extent calls)
- **ONNX maturity** (some ops still don't export cleanly)
- **Interpretability** (per-sensor error decomposition vs latent-space
  clustering)

The training data is **5 machines × 24 h @ 1 Hz × 8 sensors** =
~3.5 M raw rows, ~432 k aligned wide rows. That's small. Any of the
options below trains in **minutes on CPU, seconds on GPU**.

The hard part is **not** model size — it's:

- Multiple valid steady regimes (`IDLE`, `PRODUCTION_LIGHT`,
  `PRODUCTION_HEAVY`),
- Legitimate transients (`STARTUP`, `RAMP_UP`, `RAMP_DOWN`, `SHUTDOWN`)
  that look anomalous to a naive model,
- `OFF` periods (silence or `quality=0`).

Whichever architecture we pick, the **regime-aware sampling, augmentation
and threshold** (Phase 3 of the PLAN) matter as much as the architecture
itself.

---

## 1. Baseline already in the repo — LSTM AE

**Where**: [`notebooks/02_train_univariate_ae.ipynb`](../notebooks/02_train_univariate_ae.ipynb)
(per-sensor) and [`notebooks/03_train_multivariate_ae.ipynb`](../notebooks/03_train_multivariate_ae.ipynb)
(per-machine, all 8 sensors jointly).

**Architecture** (multivariate variant):

```
input window  (B, 64, 8)
   │
   ▼
LSTM(hidden=64)  ──►  last hidden state (B, 64)
   │
   ▼
RepeatVector(64)        (B, 64, 64)
   │
   ▼
LSTM(hidden=64, return_sequences)
   │
   ▼
TimeDistributed(Linear(8))  ──►  reconstruction (B, 64, 8)

loss   = MSE(reconstruction, input)
score  = mean MSE over the 64 timesteps
thresh = μ + K·σ  on the clean training score distribution
```

**Why it was the starting point**: smallest model that handles
multivariate temporal data, exports cleanly to ONNX (LSTM op is
well-supported since opset 14), fits in <1 MB.

**Why we want to move past it**:

- Single LSTM layer compresses the whole window into one hidden state →
  loses fine-grained temporal patterns (e.g. 60-second oscillations in
  the `hydraulic_leak` injector).
- One model trained on **all regimes mixed together** → reconstruction
  error is high during legitimate transients, causing false positives.
- Global μ + K·σ threshold → one number for `IDLE` and `PRODUCTION_HEAVY`,
  even though their error distributions are very different.
- LSTM is sequential → slower per-window on CPU than convolutions.

The baseline still works as a **sanity check** for any new architecture:
new model should match or beat it on the eval set's PR-AUC.

---

## 2. Option A — Conv1D + GRU autoencoder (recommended default)

**Idea**: stack of dilated 1-D convolutions captures local patterns at
multiple time-scales; a small GRU on top captures slower dynamics; the
decoder mirrors the encoder.

### 2.1 Sketch

```
input  (B, 64, 8)
   │
   ▼ Conv1D(ch=32, k=5, d=1) + ReLU + LayerNorm
   ▼ Conv1D(ch=32, k=5, d=2) + ReLU + LayerNorm
   ▼ Conv1D(ch=64, k=5, d=4) + ReLU + LayerNorm   ── (B, 64, 64)
   │
   ▼ GRU(hidden=64)                              ── (B, 64, 64)
   │
   ▼ Linear → latent z                           ── (B, 64, 16)
   │
   ▼ Linear → 64 channels                        ── (B, 64, 64)
   ▼ GRU(hidden=64, return_seq)                  ── (B, 64, 64)
   ▼ ConvTranspose1D(ch=32, k=5, d=4) + ReLU
   ▼ ConvTranspose1D(ch=32, k=5, d=2) + ReLU
   ▼ ConvTranspose1D(ch=8,  k=5, d=1)            ── (B, 64, 8)
```

Total parameters ≈ **150–250 k**. Trains in ~2 min on CPU, ~10 s on
a small GPU. Window of 64 samples ⇒ receptive field of ~30 samples after
the dilated stack, which covers the dominant fault timescales:

- bearing spikes (sub-second to seconds),
- hydraulic 60 s oscillation (covered by GRU),
- sensor_stuck (a flat segment vs the local context).

### 2.2 Strengths

- **Convolutions are translation-invariant**: an anomaly pattern is
  detected whether it falls early or late in the window.
- **Dilated kernels** see 1, 2, 4, 8 samples apart → multi-scale features
  without down-sampling (we keep 1 Hz resolution throughout).
- **GRU on top of conv features** is cheap (the sequence is already
  compressed by the convs) and adds long-range memory the convs lack.
- **ONNX export is rock-solid**: Conv1D, GRU, ConvTranspose1D all have
  first-class opset support and run fast in `onnxruntime` CPU.
- **Per-sensor error decomposition** for free: the output has 8 channels
  → you can report which sensor drove the anomaly score.

### 2.3 Weaknesses

- One model trained on **all regimes** still confuses transients with
  faults — must be paired with regime-aware sampling/threshold (Phase 3).
- No probabilistic interpretation of the score (just a distance).
- Latent is per-timestep, not per-window → no easy "embed-and-cluster"
  step downstream.

### 2.4 Loss / threshold recommendation

- **Loss** = `MSE(x̂, x) + λ · L1(Δx̂ − Δx)` with `λ ≈ 0.1`, where Δ is
  the first time-difference. L1 on deltas penalizes wrong dynamics
  (e.g. flat reconstruction when input is oscillating).
- **Per-sample score** = mean reconstruction MSE over the 64 timesteps,
  **per sensor**. Aggregate to a single window score with `max` over
  sensors (so single-sensor faults aren't averaged out).
- **Threshold** = 99.5° percentile of the score on the **clean
  training set**, computed **per regime** if we publish a `state`
  channel (open question #3 in the PLAN).

---

## 3. Option B — Variational Autoencoder (VAE), stretch goal

Same encoder/decoder shapes as Option A, but the encoder outputs the
parameters of a Gaussian over the latent (`μ_z, σ_z`); training adds a
**KL term** that pushes the aggregate posterior toward `N(0, I)`.

### 3.1 What it buys

- **Multimodal regimes naturally**: instead of a point latent, every
  window maps to a *distribution*; multiple modes (`IDLE` vs
  `PRODUCTION_HEAVY`) can occupy different regions of `z` without the
  decoder having to interpolate between them.
- **Two anomaly scores**, not one:
  1. **Reconstruction error** (same as AE),
  2. **`-log p(z)` under the prior**, which fires when a window's
     latent is far from any training mode — useful for *novelty* (an
     unseen operating point) that still happens to be reconstructible.
- **Generative**: can sample synthetic "plausible normal" windows, which
  is handy for sanity checks and for augmenting the rare transients.

### 3.2 What it costs

- Roughly **2× the training cost** for the same architecture (you need
  enough data + KL annealing to avoid posterior collapse).
- **KL weight is a knob** (`β-VAE`): too low ⇒ VAE behaves like AE;
  too high ⇒ blurry reconstructions and useless score.
- **ONNX export needs care**: the reparameterization
  `z = μ + σ · ε` with `ε ~ N(0,1)` must be either folded away
  (export only the deterministic mean) or handled via a `RandomNormal`
  op (supported but rare in production runtimes).
- **Two thresholds to tune** (recon + KL) — more knobs, more risk of
  miscalibration.

### 3.3 When it's worth it

- If after running Option A we see clusters of false positives that all
  correspond to **legitimate but rare** operating points (e.g. a recipe
  change), the VAE's `-log p(z)` score lets us *not* fire on them by
  thresholding only on reconstruction.
- If we want to use the latent space downstream (e.g. clustering for
  regime discovery), VAE latents are much better-behaved than AE ones.

---

## 4. Option C — Transformer encoder-decoder, GPU only

Replace the conv/GRU stack with a small Transformer (e.g. 2 encoder
layers, 2 decoder layers, `d_model=64`, 4 heads, sequence length 64).
Masked reconstruction loss, same windowing as A/B.

### 4.1 Strengths

- **Best capacity** by a wide margin: self-attention sees the whole
  window in one shot, no recurrence bottleneck.
- **Handles variable-length context** elegantly (if we later want to
  feed 128 or 256 samples without retraining the architecture).
- State of the art on most public time-series anomaly benchmarks
  (e.g. Anomaly Transformer, TranAD, USAD).

### 4.2 Costs

- ~10× more parameters than Option A for the same window size; needs
  more data or strong regularization, otherwise it overfits the few
  hours of training we have.
- **GPU strongly recommended** for training; inference on CPU is
  feasible but ~5–10× slower per window than Conv1D+GRU — relevant for
  KQL `python()` calls that score per ingested extent.
- **ONNX**: works (PyTorch → ONNX → onnxruntime), but attention masks
  and positional encodings sometimes export with shape-inference
  warnings; needs explicit testing.
- Most of the architectural value (long context) is wasted on our
  64-sample windows.

### 4.3 When it's worth it

- Only if **open question #4** in the PLAN resolves with "GPU is
  available", and only after Option A has been shown to *not* hit the
  target PR-AUC on the eval set. Otherwise it's overkill.

---

## 5. Cheaper baselines, kept for comparison

These won't be the main model but are good sanity checks because
they're trivial to train and explain.

### 5.1 Isolation Forest on hand-crafted features

For each window of 64 samples, compute per-sensor `mean`, `std`,
`min`, `max`, `slope`, `dominant_freq` → fixed-length vector → fit
`sklearn.ensemble.IsolationForest`. Already documented in
[`docs/anomaly_detection_fabric_kql.md`](anomaly_detection_fabric_kql.md#L290).

- **Pros**: trains in seconds, no GPU, ONNX export via `skl2onnx`
  works, good lower bound on detection performance.
- **Cons**: hand-crafted features mean we have to decide a priori
  what's "interesting" — likely to miss the `hydraulic_leak`
  oscillation if its frequency wasn't in the feature set.

### 5.2 KQL native — `series_decompose_anomalies()`

Pure KQL, no model artifact. Seasonal decomposition + IQR test on the
residual. Free, no training.

- **Pros**: zero infrastructure cost, one line of KQL.
- **Cons**: per-sensor only, no multivariate context, no notion of
  regime — bad at everything except very obvious spikes. Useful only
  as a "did *anything* fire?" co-pilot to the real model.

---

## 6. Comparison matrix

| Option | Params | Train (CPU) | Inference / window (CPU) | ONNX maturity | Multimodal regimes | Per-sensor explain |
|---|---|---|---|---|---|---|
| LSTM AE (baseline)   | ~30 k  | ~1 min  | ~3 ms  | ★★★ | ✗ | ✓ |
| **Conv1D + GRU AE**  | ~200 k | ~2 min  | ~2 ms  | ★★★ | △ (with regime threshold) | ✓ |
| VAE (same backbone)  | ~220 k | ~4 min  | ~3 ms  | ★★  | ✓ | ✓ |
| Transformer enc-dec  | ~2 M   | ~30 min CPU / ~2 min GPU | ~15 ms | ★★ | ✓ | △ |
| IForest + features   | n/a    | ~5 s    | <1 ms  | ★★★ | ✗ | ✗ (feature-level only) |
| KQL `series_decompose_anomalies` | 0 | 0 | n/a (in-KQL) | n/a | ✗ | ✗ |

Legend: ★★★ = production-proven, ★★ = works but needs testing,
✓ / △ / ✗ = supported / partial / not really.

---

## 7. Recommended path forward

1. **Build the new notebook around Option A (Conv1D + GRU AE)** — it
   gives a clear improvement over the LSTM baseline without needing a
   GPU, exports cleanly to ONNX, and lets us validate the *training +
   evaluation* pipeline (regime-aware sampling, per-fault PR-AUC,
   detection delay, ONNX export, KQL scorer round-trip) before
   investing in a bigger model.
2. Train on `data/training/telemetry_wide.parquet` only.
3. Evaluate on `data/eval/telemetry_wide.parquet` using
   `data/eval/anomaly_labels.parquet` as ground truth:
   - PR-AUC per fault family (`bearing`, `hydraulic_leak`,
     `sensor_stuck`),
   - Median **detection delay** (seconds between `onset_ts` and first
     window with `score > threshold`),
   - False-positive rate on clean machines `M-101`, `M-102`.
4. **If** Option A misses on one specific failure mode (e.g. the slow
   `hydraulic_leak` ramps): add VAE (Option B) on top of the same
   backbone before considering a Transformer.
5. **If** the FP rate on transients is the bottleneck: that's a
   *threshold/regime* problem, not an architecture problem — fix it in
   the data pipeline (Phase 3 of the PLAN), not by swapping models.

## 8. Reading list

- Malhotra et al., *LSTM-based Encoder-Decoder for Multi-sensor Anomaly
  Detection*, 2016 — the original LSTM AE for industrial sensors.
- Audibert et al., *USAD: UnSupervised Anomaly Detection on Multivariate
  Time Series*, KDD 2020 — adversarial AE, good comparison baseline.
- An & Cho, *Variational Autoencoder based Anomaly Detection using
  Reconstruction Probability*, 2015 — the VAE-for-AD reference.
- Xu et al., *Anomaly Transformer*, ICLR 2022 — Transformer SOTA on
  public benchmarks.
- Goldstein & Uchida, *A Comparative Evaluation of Unsupervised Anomaly
  Detection Algorithms*, 2016 — sober reminder that Isolation Forest
  is hard to beat on tabular features.
