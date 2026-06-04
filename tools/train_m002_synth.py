"""Train the M-002 anomaly model on the synthgen replay trace and export ONNX.

M-002 is a synthgen-driven CNC spindle (see tools/build_synth_trace.py): the
simulator replays a synthetic 1 Hz trace of the three spindle signals
(mandrino_load, mandrino_power, mandrino_torque). This script trains the
anomaly model on *exactly that trace* (``_local/synthgen/synth_trace_full.npz``)
so the model the scorer runs is consistent with what the simulator serves
(train/serve consistency, identical to the M-003 path).

The architecture, ONNX export and baked-in score are shared with
tools/train_cnc_m003.py (imported below); only the data source and the model
id differ.

Usage:
    .venv/Scripts/python.exe tools/train_m002_synth.py
    .venv/Scripts/python.exe tools/train_m002_synth.py --epochs 24
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import sys
import time
from pathlib import Path

import numpy as np
import onnx  # noqa: F401  (kept for parity with the M-003 trainer)
import onnxruntime as ort
import torch
from torch.utils.data import DataLoader, TensorDataset

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
from train_cnc_m003 import (  # noqa: E402
    D_MODEL,
    FF_DIM,
    KUSTO_ROW_BUDGET_BYTES,
    N_DEC,
    N_ENC,
    N_HEADS,
    SANDBOX_IR_VERSION,
    SEED,
    STRIDE_EVAL,
    STRIDE_TRAIN,
    WEIGHT_DECAY,
    WINDOW,
    ScoreWrapper,
    ScoreWrapperFP16,
    TransformerAE,
    build_windows,
    composite_loss,
    export_onnx_single_file,
    score_windows,
)

MACHINE = "M-002"
MODEL_NAME = f"transformer_ae_small__{MACHINE}"
TRACE_PATH = REPO / "_local" / "synthgen" / "synth_trace_full.npz"
UNITS = {"mandrino_load": "%", "mandrino_power": "kW", "mandrino_torque": "N*cm"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", type=str, default=str(TRACE_PATH),
                    help="Path to the synthgen full trace .npz (values/active/sensors).")
    ap.add_argument("--epochs", type=int, default=24)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--threshold-quantile", type=float, default=0.995)
    args = ap.parse_args(argv)

    trace_path = Path(args.trace)
    if not trace_path.exists():
        print(f"ERROR: synthgen trace not found: {trace_path}\n"
              f"Run tools/build_synth_trace.py first.", file=sys.stderr)
        return 2

    data = np.load(trace_path, allow_pickle=True)
    sensors = [str(s) for s in data["sensors"].tolist()]
    vals = data["values"].astype(np.float32)
    mask = data["active"].astype(bool)
    n_features = len(sensors)
    np.random.seed(SEED)
    torch.manual_seed(SEED)

    active = vals[mask]
    print(f"[gen] trace: {len(vals):,} steps, active {len(active):,} "
          f"({mask.mean():.1%} duty), sensors={sensors}")

    mean = active.mean(axis=0).astype(np.float32)
    std = active.std(axis=0)
    std[std == 0] = 1.0
    std = std.astype(np.float32)
    arr = ((active - mean) / std).astype(np.float32)

    X = build_windows(arr, STRIDE_TRAIN, n_features)
    print(f"[train] training windows: {X.shape}")

    rng = np.random.default_rng(SEED)
    idx = rng.permutation(len(X))
    cut = int(len(X) * 0.85)
    tr_idx, va_idx = idx[:cut], idx[cut:]
    X_tr = torch.from_numpy(X[tr_idx])
    X_va = torch.from_numpy(X[va_idx])

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[train] device={device}")
    dl_tr = DataLoader(TensorDataset(X_tr), batch_size=args.batch, shuffle=True, drop_last=True)
    dl_va = DataLoader(TensorDataset(X_va), batch_size=args.batch, shuffle=False)

    model = TransformerAE(n_features).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"[train] parameters: {n_params:,}")
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=WEIGHT_DECAY)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs * max(1, len(dl_tr)))

    best_val, best_state = float("inf"), None
    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        model.train()
        tr_running, n_seen = 0.0, 0
        for (xb,) in dl_tr:
            xb = xb.to(device)
            opt.zero_grad(set_to_none=True)
            loss = composite_loss(model(xb), xb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            tr_running += loss.item() * xb.size(0)
            n_seen += xb.size(0)
        model.eval()
        va_running, n_va = 0.0, 0
        with torch.no_grad():
            for (xb,) in dl_va:
                xb = xb.to(device)
                va_running += composite_loss(model(xb), xb).item() * xb.size(0)
                n_va += xb.size(0)
        va_loss = va_running / n_va
        flag = ""
        if va_loss < best_val:
            best_val = va_loss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            flag = "  <- best"
        print(f"[train] epoch {epoch:2d}/{args.epochs}  train={tr_running/n_seen:.5f}  "
              f"val={va_loss:.5f}  ({time.time()-t0:.1f}s){flag}")

    assert best_state is not None
    model.load_state_dict(best_state)
    model.eval()

    val_scores = score_windows(model, X[va_idx], device)
    threshold = float(np.quantile(val_scores, args.threshold_quantile))
    print(f"[train] threshold (p{args.threshold_quantile*100:g}): {threshold:.5f}")

    art_dir = REPO / "models" / MODEL_NAME
    art_dir.mkdir(parents=True, exist_ok=True)
    onnx_fp32 = art_dir / "model.onnx"
    onnx_fp16 = art_dir / "model.fp16.onnx"
    dummy = torch.randn(1, WINDOW, n_features, dtype=torch.float32)

    export_model = ScoreWrapper(copy.deepcopy(model).to("cpu").eval()).eval()
    export_onnx_single_file(export_model, dummy, onnx_fp32)

    fp16_model = ScoreWrapperFP16(copy.deepcopy(model).to("cpu").eval()).eval()
    export_onnx_single_file(fp16_model, dummy, onnx_fp16)

    def size_report(path: Path) -> dict:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw)
        return {"path": path.name, "raw_kb": round(len(raw)/1024, 1),
                "base64_kb": round(len(b64)/1024, 1),
                "kusto_row_fits": len(b64) <= KUSTO_ROW_BUDGET_BYTES}

    fp32_info, fp16_info = size_report(onnx_fp32), size_report(onnx_fp16)
    print(f"[train] FP16  raw={fp16_info['raw_kb']} KB  b64={fp16_info['base64_kb']} KB  "
          f"fits={fp16_info['kusto_row_fits']}")

    sample = X[va_idx[:128]].astype(np.float32)
    with torch.no_grad():
        torch_scores = export_model(torch.from_numpy(sample)).numpy()
    ort_fp16 = ort.InferenceSession(onnx_fp16.as_posix(), providers=["CPUExecutionProvider"]).run(
        ["score"], {"window": sample})[0]
    rel = float(np.max(np.abs(ort_fp16 - torch_scores) / np.maximum(np.abs(torch_scores), 1e-6)))
    print(f"[train] FP16 ONNX vs PyTorch max |rel diff|: {rel:.2%}")

    scaler = {"sensors": sensors, "mean": [float(x) for x in mean], "std": [float(x) for x in std]}
    (art_dir / "scaler.json").write_text(json.dumps(scaler, indent=2))
    meta = {
        "model": MODEL_NAME, "machine_id": MACHINE, "window": WINDOW,
        "stride_train": STRIDE_TRAIN, "stride_eval": STRIDE_EVAL,
        "n_features": n_features, "d_model": D_MODEL, "n_heads": N_HEADS,
        "n_enc_layers": N_ENC, "n_dec_layers": N_DEC, "ff_dim": FF_DIM,
        "n_parameters": n_params, "threshold": threshold,
        "threshold_rule": f"p{args.threshold_quantile*100:g} on training-val window scores",
        "data_source": "synthgen_trace (_local/synthgen/synth_trace_full.npz)",
        "units": UNITS,
        "training": {"epochs": args.epochs, "batch": args.batch, "lr": args.lr,
                     "weight_decay": WEIGHT_DECAY, "best_val_loss": best_val,
                     "device": str(device), "duty_cycle": float(mask.mean())},
        "onnx": {"ir_version": SANDBOX_IR_VERSION, "fp32": fp32_info, "fp16": fp16_info,
                 "parity_fp16_max_rel_diff": rel},
        "scaler": scaler,
        "kusto_deployable": fp16_info["kusto_row_fits"],
    }
    (art_dir / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    torch.save(model.state_dict(), art_dir / "model.pt")

    print(f"[train] artifacts in models/{MODEL_NAME}:")
    for p in sorted(art_dir.iterdir()):
        print(f"  {p.name:>20s}   {p.stat().st_size/1024:>8.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
