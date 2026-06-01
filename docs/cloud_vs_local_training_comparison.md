# Cloud (Azure ML GPU) vs Local (CPU) training — M-004 benchmark

**Date:** 2026-06-01
**Model:** `transformer_ae_small` (TransformerAE autoencoder)
**Machine trained:** `M-004` (synthetic, 8 sensors, 8 h of data, seed 1337)
**Training script (identical for both runs):**
[cloud-training/src/generate_and_train.py](../cloud-training/src/generate_and_train.py)
— same code, same args `--machines M-004 --hours 8.0 --epochs 12`.

The *only* difference between the two runs is the execution environment:
an Azure ML **Tesla T4 GPU** node vs the **local Windows ARM64 CPU**.

Raw timing records:
[_local/timing_m004_cloud.json](../_local/timing_m004_cloud.json),
[_local/timing_m004_local.json](../_local/timing_m004_local.json).

---

## 1. GPU quota availability (prerequisite check)

The user asked to confirm GPU quota in **both** `italynorth` and `westeurope`.

> **Key distinction:** `az vm list-usage` reports *regional subscription VM*
> quota, where `Standard NCASv3_T4 Family = 0` in both regions. But Azure ML
> compute clusters draw from a **separate "Cluster Dedicated vCPUs" (BatchAI)
> quota**, queried via the MachineLearningServices REST endpoint. That is the
> authoritative number for AML provisioning.

AML dedicated-core quota (`.../locations/{loc}/quotas?api-version=2024-04-01`):

| Region | GPU family | Limit (cores) |
|---|---|---|
| **italynorth** | Standard NCASv3_T4 (Tesla T4) | **16** |
| **westeurope** | Standard NCASv3_T4 (Tesla T4) | **16** |
| westeurope | standardNCFamily (K80) | 100 |
| westeurope | standardNVFamily (M60) | 100 |

✅ T4 GPU quota (16 cores) is available in **both** regions. The benchmark ran
in **westeurope** (where the `anomalyml-mlw` workspace lives) on a
`Standard_NC4as_T4_v3` node (1× Tesla T4, 4 vCPU — well within the 16-core
limit).

---

## 2. Hardware & environment

| | Cloud (Azure ML) | Local |
|---|---|---|
| Device | **Tesla T4** GPU (16 GB) | **Qualcomm ARMv8** (Snapdragon, 12 threads) |
| SKU / host | `Standard_NC4as_T4_v3` (4 vCPU) | Windows 11 ARM64 laptop |
| PyTorch | `2.3.1+cu121` (CUDA) | `2.11.0+cpu` |
| Python | 3.11 (conda env) | 3.13.13 (venv) |
| Compute cost | Per-second GPU node (scales to 0 when idle) | Free (already-owned hardware) |

Model is identical: WINDOW=64, D_MODEL=56, 4 heads, 2+2 enc/dec layers,
FF=160, **161,984 parameters**.

---

## 3. End-to-end timing

### Cloud (Azure ML GPU) — phase breakdown

| Phase | Seconds | One-time? |
|---|---:|---|
| Cluster provision (create `gpu-t4-cluster` from scratch) | **39.0** | one-time (cluster reused afterwards) |
| Job submit (`create_or_update`) | 19.8 | per-run |
| Queue → Running (node cold-start from 0 + **first-time conda env build**) | ~214 | mostly one-time (env image is cached) |
| Running → Completed (container start + data-gen + train + ONNX export + upload) | ~208 | per-run |
| Artifact download | 6.5 | per-run |
| **End-to-end** | **487.2** (≈ 8.1 min) | |

Status transitions (UTC): Queued `11:19:07` → Running `11:22:37` → Completed `11:26:04`.

### Local (CPU) — end-to-end

| Phase | Seconds |
|---|---:|
| Data generation (28,800 ticks) + 12 epochs + ONNX export | — |
| **End-to-end** | **143.5** (≈ 2.4 min) |

### Head-to-head

| Metric | Cloud (T4) | Local (CPU) | Ratio |
|---|---:|---:|---:|
| **End-to-end wall-clock** | **487.2 s** | **143.5 s** | local **3.4× faster** |
| Per-epoch (mean) | **0.14 s** | **9.43 s** | GPU **~67× faster** |
| 12-epoch training compute | ~1.7 s | ~113 s | GPU **~66× faster** |
| First epoch (warmup) | 0.4 s | 9.8 s | |

---

## 4. Analysis — why local wins end-to-end here

The Tesla T4 crushes the CPU on **raw compute** (~67× faster per epoch), but
for this **tiny** model the actual training is only ~1.7 s on GPU. The cloud
run is dominated by **fixed orchestration overhead** that the local run simply
doesn't have:

```
Cloud 487 s  =  39 s cluster create  +  20 s submit  +  ~214 s node
               cold-start & env build  +  ~208 s container/script/upload  +  7 s download
                └──────────── overhead (~480 s) ────────────┘   └ ~2 s GPU compute ┘

Local 143 s  =  ~30 s data-gen  +  ~113 s CPU training  +  export
               (zero orchestration overhead)
```

So for a 162 k-parameter model trained for 12 epochs, the GPU's compute
advantage (saving ~111 s) is swamped by ~480 s of cloud fixed cost. **Local
wins by 3.4×.**

### What changes the verdict

The cloud overhead is **largely one-time / amortizable**:

- **Cluster creation (39 s)** happens once — the cluster persists and
  `min_instances=0`, so it costs nothing while idle.
- **Conda env build (bulk of the ~214 s)** is cached — subsequent jobs reuse
  the built image, cutting the queue→running phase to roughly the node
  cold-start (~1–2 min), or near-zero if a warm node is kept (`min_instances=1`).
- A **second** M-004-sized job on the warm cluster would land around
  **~3–4 min** end-to-end (vs 2.4 min local) — still local-favoured for this
  model size.

Cloud GPU becomes the clear winner when:

- the model is **large** (millions of params) or training runs **many epochs /
  large datasets**, where the ~67× per-epoch speed-up dominates the fixed cost;
- you train a **fleet** of machines in one job (overhead amortized across N models);
- the local machine lacks a capable GPU (here: ARM64 CPU only).

Local is better when:

- iterating on **small models** (like this one) where end-to-end latency and
  zero per-run cost matter most;
- doing quick experiments / debugging the training code.

---

## 5. Result parity & a portability finding

Both runs produced essentially identical training curves (best val loss
≈ 0.158, threshold ≈ 1.47), confirming the runs are equivalent modulo
GPU/CPU floating-point nondeterminism.

| Output | Cloud (T4, torch 2.3.1) | Local (ARM64, torch 2.11.0) |
|---|---|---|
| 12-epoch training | ✅ | ✅ |
| FP32 + FP16 ONNX export | ✅ | ✅ (graph produced) |
| **FP16 ONNX verification** | ✅ parity 0.23%, Kusto-deployable (657 KB b64) | ❌ failed |

> **Finding (recorded as a gotcha):** the local **torch 2.11.0** ONNX
> exporter on ARM64 produced an FP16 graph that **onnxruntime** could not run —
> `Reshape … input {64,128,56} → requested {64,56}` (a `gemm_input_reshape`
> mismatch). The cloud's **torch 2.3.1** produced a valid, Kusto-deployable
> FP16 model. The *training* is environment-independent; the *ONNX export +
> verification* is sensitive to the torch version. For producing the
> deployable artifact, **the cloud run (torch 2.3.1) is the source of truth** —
> its `transformer_ae_small__M-004/` is what was kept in
> [models/transformer_ae_small__M-004](../models/transformer_ae_small__M-004).

---

## 6. Recommendation

For this anomaly-detection demo (small per-machine TransformerAE models,
≤ 1 MB Kusto-deployable), **local CPU training is the most efficient choice**
for single-machine iteration: ~2.4 min end-to-end, no cost, no orchestration.

**Reserve the Azure ML T4 GPU for:**
1. Batch/fleet retraining of **many machines in one job** (overhead amortized).
2. **Larger / deeper** models or longer training schedules.
3. Producing the **canonical deployable ONNX artifact** (stable torch 2.3.1
   toolchain), avoiding the local ARM64 FP16 export incompatibility.

The `gpu-t4-cluster` (`min_instances=0`) **scales to zero and incurs no cost
when idle** — it can be left in place for on-demand GPU runs.

---

## 7. Reproduction

```powershell
# Quota check (both regions)
.\.venv\Scripts\python.exe _local/_check_aml_quota.py

# Cloud GPU run (creates cluster if missing, submits, waits, downloads)
.\.venv\Scripts\python.exe _local/_train_m004_cloud.py

# Local CPU run (identical script + args)
.\.venv\Scripts\python.exe _local/_train_m004_local.py
```
