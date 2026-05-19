# Model deployment options for the Fabric KQL anomaly pipeline

Companion to [`model_architecture_options.md`](model_architecture_options.md).
That document is about **which model to build**; this one is about
**how and where to run it for inference**, given the Fabric KQL
constraints.

Status as of this writing (May 2026):

| Model | File | Raw ONNX | Base64 | IR | Single-file? | Fits 1 MB Kusto row? |
|---|---|---|---|---|---|---|
| Conv+GRU v2 FP32 | `models/conv_gru_ae/model.onnx` | 651 KB | 868 KB | 9 | yes | yes (tight) |
| **Conv+GRU v2 FP16** | `models/conv_gru_ae/model.fp16.onnx` | **334 KB** | **446 KB** | **9** | **yes** | **yes (with margin)** |
| Transformer FP32 | `models/transformer_ae/model.onnx` (+ `.data`) | 882 KB + 698 KB | n/a | 10 | **no** (external weights file) | **no** |

The Transformer is currently **not deployable** in any of the
KQL-friendly patterns below, because the dynamo exporter spilled its
weights into a sidecar `.data` file. Until it is re-exported as a
single self-contained ONNX (legacy tracer + FP16), only Conv+GRU v2 is
production-ready.

---

## 1. The deployment surface

The Kusto Python plugin loads ONNX models in one of two ways:

| Pattern | Where the bytes live | Kusto-side limit | Per-batch cost |
|---|---|---|---|
| **Inline** | base64 string in a row of the `models` table | ~1 MB per cell | none |
| **External artifact** | blob URL declared in the `python(...)` call's `external_artifacts` | ~1 GB sandbox limit | blob fetch on every invocation |

`external_artifacts` is part of the **query compile**, not data. Kusto
pre-fetches every URL listed there on each Python sandbox spin-up —
**including artifacts the Python code does not end up using**. There is
no way to conditionalise it from inside KQL based on row data.

This single property is the hinge for every pattern below.

---

## 2. The three switch patterns we considered

We want to be able to **run two models** (Conv+GRU v2 and Transformer)
and switch which one is active without rebuilding the pipeline.

### Pattern A — Single function, data-driven switch, both URLs in `external_artifacts`

One scoring function reads an `active_model` table; both the inline
payload and the blob URL are always available. Python dispatches to the
active one.

- **Switching**: one row insert into `active_model`. Instant. Reversible.
- **Cost**: blob fetched on **every** Python invocation, regardless of
  which model is active. ~50–200 ms latency + continuous egress.
- **Verdict**: **rejected** — wastes performance and money when the
  inline model is active, which is meant to be the steady state.

### Pattern B — Two scoring functions, swap via update-policy DDL

`score_anomaly_inline()` and `score_anomaly_external()` exist as
separate functions. Switch by re-running `.alter table ... policy update`
pointing at the other function.

- **Switching**: admin DDL, can be wrapped in a small PowerShell helper
  (`Set-ActiveModel.ps1`).
- **Cost**: zero per-batch overhead for whichever model is active.
  External model still costs blob egress + ~50–200 ms whenever active,
  because its size is intrinsic.
- **Verdict**: **acceptable fallback**, not first choice.

### Pattern C — Both models inline, one row each, switch by picking the row

Each model lives as its own row in the `models` table. A small
`active_model` table holds a pointer. The scoring function picks the
correct row at query time.

- **Switching**: one row insert into `active_model`. Instant.
  Reversible. No DDL. No admin permissions at switch time.
- **Cost**: zero. No blob storage account, no SAS rotation, no per-batch
  egress, no download latency.
- **Constraint**: every model must fit in a single Kusto row
  (~1 MB base64, IR ≤ 9, single-file ONNX).
- **Verdict**: **first choice**, *if* the Transformer can be made to fit.

---

## 3. The hinge: does the Transformer fit in a row?

The current Transformer is 180,808 params. Sizes:

- FP32 raw weights only: ~720 KB
- FP32 ONNX, single file (legacy tracer): ~750–800 KB raw → ~1000–1070 KB base64 → **at or just over** the 1 MB cap
- **FP16 ONNX, single file**: ~400–450 KB raw → ~530–600 KB base64 → **fits comfortably**

FP16 conversion is the same recipe already used in
`notebooks/05_train_conv_gru_ae.ipynb` (via `onnxconverter_common`,
`keep_io_types=True`). On Conv+GRU v2 it produced max relative diff of
**0.12 %** vs FP32 — well below thresholding noise. The Transformer is
expected to behave similarly because its dominant ops (MatMul, GEMM,
LayerNorm) are well-supported in FP16.

If FP16 fits → Pattern C is achievable as-is, no architecture change.

If FP16 still does **not** fit (unlikely but possible due to graph
overhead), the fallback path is in section 4.

---

## 4. What if the Transformer cannot fit in a row?

Three options, in increasing complexity:

### 4.1 Shrink the Transformer (preferred)

Drop `D_MODEL` from 64 → 32, or drop one encoder layer. ~50% fewer
parameters (~90k). Retrain (~minutes on the RTX 2070 SUPER). FP16 ONNX
falls to ~250 KB raw, fits with huge margin.

- **Cost**: zero new infra. Same Pattern C deploy, same KQL.
- **Risk**: PR-AUC drops; if it falls to ~Conv+GRU level (~0.47), the
  Transformer becomes pointless and we just keep Conv+GRU.

### 4.2 Hot/cold tier (production-grade)

The Transformer never enters the streaming hot path.

- **Hot path**: Conv+GRU v2 inline in the KQL update policy, scores
  every event in <1 s. Writes to `anomalies_hot`.
- **Cold path**: Fabric Spark notebook (or Real-Time Intelligence
  reflex) on a 1–5 min schedule. Reads the recent window from KQL, runs
  the full Transformer (model lives on OneLake, no row size limit),
  writes `anomalies_refined`.
- Dashboards overlay the two: hot fires alerts, cold confirms or
  downgrades them.

- **Cost**: one scheduled Spark notebook. Bounded by schedule, not
  event rate. The streaming critical path stays cheap and predictable.
- **Best fit** for the "performance + cost first" constraint: the
  expensive model can grow without bound without ever loading on the
  hot path.

### 4.3 External compute for all scoring (overkill today)

Eventstream → containerized scorer (Azure Function, Container App,
Spark Structured Streaming) → write back to KQL. KQL becomes pure
storage + dashboarding.

- **Cost**: meaningful. Always-on compute, networking, monitoring,
  deploys. New failure surface.
- **When justified**: only if model zoo > 2–3, model size > ~100 MB,
  GPU inference needed, or complex A/B routing. None of these apply to
  the current scope.

---

## 5. Decision tree

```
Is the Transformer needed at all? (i.e. PR-AUC 0.56 vs Conv+GRU 0.47 matters)
├── NO  → deploy only Conv+GRU v2 inline; Pattern C reduces to one row. Done.
└── YES → re-export Transformer as single-file FP16 ONNX, IR=9, measure.
         ├── fits in row → Pattern C (both inline, swap by row). DONE.
         └── does not fit → shrink Transformer (4.1) and re-measure.
                ├── fits AND accuracy OK → Pattern C.
                ├── fits but accuracy collapses → hot/cold tier (4.2).
                └── still does not fit → hot/cold tier (4.2).
```

---

## 6. Hard rules locked in by this analysis

These are not negotiable in any deployment we do from here:

1. Every ONNX shipped to KQL has **`ir_version ≤ 9`** (sandbox
   `onnxruntime` requirement; same hack used in notebook 02).
2. Every ONNX shipped to KQL is a **single file** (no `.onnx.data`
   sidecar). The dynamo exporter must be disabled (`dynamo=False`) or
   we re-pack with `onnx.save(..., save_as_external_data=False)`.
3. Steady-state scoring path must not declare `external_artifacts`
   unless the active model actually requires it. The always-fetch
   penalty is unacceptable under the performance/cost constraint.
4. Models table rows must include enough metadata
   (`window`, `sensors`, `mean`, `std`, `threshold`) to score without
   any KQL-side configuration: the function pulls everything from the
   row.

---

## 7. Open items for next session

- [ ] Re-export Transformer as single-file FP16 ONNX (IR=9), measure
      base64 size, verify parity vs FP32 PyTorch reference.
- [ ] If fits → proceed with Pattern C: define `models` table schema,
      `active_model` table, single `score_anomaly()` function.
- [ ] If does not fit → train shrunken Transformer
      (`D_MODEL=32` or one fewer encoder layer), re-measure.
- [ ] Document the row schema decision in
      [`model_architecture_options.md`](model_architecture_options.md)
      §7 so the two docs stay consistent.
- [ ] Keep `simulator-cloud/` and the existing KQL DDL untouched until
      the deployment shape above is committed.
