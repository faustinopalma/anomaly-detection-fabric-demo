"""Train one `transformer_ae_small` model for a single machine.

Reads the existing wide training parquet, filters to one machine_id,
fits StandardScaler + AE, exports FP16 ONNX (KQL-deployable) and
writes artifacts under ``models/transformer_ae_small__<MACHINE>/``.

Threshold is calibrated as p99.5 of the per-window scores on the
training-validation split (no separate eval set is required for the
2-machine demo; live correlation drives operational metrics).

Usage:
    python tools/train_per_machine.py --machine M-001
    python tools/train_per_machine.py --machine M-002 --epochs 12
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
import onnx
import onnxruntime as ort
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_TRAIN = REPO_ROOT / "data" / "training" / "telemetry_wide.parquet"

SENSORS = [
    "vibration_axial", "vibration_radial",
    "temperature_motor", "temperature_bearing",
    "pressure_hydraulic", "current", "power", "spindle_rpm",
]
N_FEATURES = len(SENSORS)

# Architecture: identical to notebook 06.
WINDOW = 64
STRIDE_TRAIN = 16
STRIDE_EVAL = 1
D_MODEL = 56
N_HEADS = 4
N_ENC = 2
N_DEC = 2
FF_DIM = 160
DROPOUT = 0.1

# Defaults; can be overridden via CLI.
BATCH = 256
EPOCHS = 12
LR = 3e-4
WEIGHT_DECAY = 1e-5
SEED = 1337

SANDBOX_IR_VERSION = 9
KUSTO_ROW_BUDGET_BYTES = 1_048_576


class TransformerAE(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.window = WINDOW
        self.input_proj = nn.Linear(N_FEATURES, D_MODEL)
        self.output_head = nn.Linear(D_MODEL, N_FEATURES)
        self.pos_enc = nn.Parameter(torch.randn(1, WINDOW, D_MODEL) * 0.02)
        self.pos_dec = nn.Parameter(torch.randn(1, WINDOW, D_MODEL) * 0.02)
        self.query = nn.Parameter(torch.randn(1, WINDOW, D_MODEL) * 0.02)
        enc_layer = nn.TransformerEncoderLayer(
            d_model=D_MODEL, nhead=N_HEADS, dim_feedforward=FF_DIM,
            dropout=DROPOUT, batch_first=True, activation="gelu",
            norm_first=True,
        )
        self.encoder = nn.TransformerEncoder(enc_layer, num_layers=N_ENC)
        dec_layer = nn.TransformerDecoderLayer(
            d_model=D_MODEL, nhead=N_HEADS, dim_feedforward=FF_DIM,
            dropout=DROPOUT, batch_first=True, activation="gelu",
            norm_first=True,
        )
        self.decoder = nn.TransformerDecoder(dec_layer, num_layers=N_DEC)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.input_proj(x) + self.pos_enc
        memory = self.encoder(h)
        q = self.query.expand(x.shape[0], -1, -1) + self.pos_dec
        y = self.decoder(q, memory)
        return self.output_head(y)


class ScoreWrapper(nn.Module):
    def __init__(self, ae: nn.Module) -> None:
        super().__init__()
        self.ae = ae

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x_hat = self.ae(x)
        err = (x_hat - x) ** 2
        per_sensor = err.mean(dim=1)
        return per_sensor.max(dim=1).values


class ScoreWrapperFP16(nn.Module):
    def __init__(self, ae: nn.Module) -> None:
        super().__init__()
        self.ae = ae.half()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x16 = x.half()
        x_hat = self.ae(x16)
        err = (x_hat - x16) ** 2
        per_sensor = err.mean(dim=1)
        return per_sensor.max(dim=1).values.float()


def composite_loss(x_hat: torch.Tensor, x: torch.Tensor, lam_delta: float = 0.1) -> torch.Tensor:
    mse = torch.mean((x_hat - x) ** 2)
    dx_hat = x_hat[:, 1:, :] - x_hat[:, :-1, :]
    dx = x[:, 1:, :] - x[:, :-1, :]
    l1d = torch.mean(torch.abs(dx_hat - dx))
    return mse + lam_delta * l1d


def drop_off(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["state"] != "OFF"].copy()


def build_windows(arr: np.ndarray, stride: int) -> np.ndarray:
    n = arr.shape[0]
    if n < WINDOW:
        return np.empty((0, WINDOW, N_FEATURES), dtype=np.float32)
    out = np.lib.stride_tricks.sliding_window_view(arr, window_shape=WINDOW, axis=0)[::stride]
    return np.ascontiguousarray(out.transpose(0, 2, 1))


def split_train_val(X: np.ndarray, val_frac: float = 0.15, seed: int = SEED):
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(X))
    cut = int(len(X) * (1 - val_frac))
    return idx[:cut], idx[cut:]


@torch.no_grad()
def score_windows(model: nn.Module, X: np.ndarray, device: torch.device, batch: int = 2048) -> np.ndarray:
    model.eval()
    out = np.empty(len(X), dtype=np.float32)
    for i in range(0, len(X), batch):
        xb = torch.from_numpy(X[i:i + batch]).to(device, non_blocking=True)
        x_hat = model(xb)
        err = (x_hat - xb) ** 2
        per_sensor = err.mean(dim=1)
        out[i:i + batch] = per_sensor.max(dim=1).values.detach().cpu().numpy()
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--machine", required=True, help="e.g. M-001")
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--lr", type=float, default=LR)
    ap.add_argument("--threshold-quantile", type=float, default=0.995)
    args = ap.parse_args(argv)

    machine = args.machine
    model_name = f"transformer_ae_small__{machine}"
    art_dir = REPO_ROOT / "models" / model_name
    art_dir.mkdir(parents=True, exist_ok=True)

    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[train] machine={machine}  device={device}")
    if device.type == "cuda":
        print(f"[train] gpu={torch.cuda.get_device_name(0)}")

    print(f"[train] loading {DATA_TRAIN.relative_to(REPO_ROOT)} ...")
    df = pd.read_parquet(DATA_TRAIN)
    df = df[df["machineId"] == machine]
    if df.empty:
        print(f"[train] ERROR: no rows for {machine} in {DATA_TRAIN}", file=sys.stderr)
        return 2
    df = drop_off(df).sort_values(["machineId", "ts"]).reset_index(drop=True)
    print(f"[train] rows after drop OFF: {len(df):,}")

    # Per-machine scaler.
    mean = df[SENSORS].mean(axis=0).astype(np.float32).values
    std = df[SENSORS].std(axis=0).replace(0, 1.0).astype(np.float32).values
    arr = ((df[SENSORS].to_numpy(dtype=np.float32) - mean) / std).astype(np.float32)

    X_train = build_windows(arr, STRIDE_TRAIN)
    print(f"[train] training windows : {X_train.shape}")

    tr_idx, va_idx = split_train_val(X_train)
    X_tr = torch.from_numpy(X_train[tr_idx])
    X_va = torch.from_numpy(X_train[va_idx])

    dl_tr = DataLoader(TensorDataset(X_tr), batch_size=args.batch, shuffle=True,
                       pin_memory=(device.type == "cuda"), drop_last=True)
    dl_va = DataLoader(TensorDataset(X_va), batch_size=args.batch, shuffle=False,
                       pin_memory=(device.type == "cuda"))
    print(f"[train] train batches: {len(dl_tr):,} ({len(X_tr):,} windows)")
    print(f"[train] val   batches: {len(dl_va):,} ({len(X_va):,} windows)")

    model = TransformerAE().to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"[train] parameters: {n_params:,}")

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=WEIGHT_DECAY)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs * max(1, len(dl_tr)))

    best_val = float("inf")
    best_state = None
    for epoch in range(1, args.epochs + 1):
        t0 = time.time()
        model.train()
        tr_running, n_seen = 0.0, 0
        for (xb,) in dl_tr:
            xb = xb.to(device, non_blocking=True)
            opt.zero_grad(set_to_none=True)
            x_hat = model(xb)
            loss = composite_loss(x_hat, xb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            opt.step()
            sched.step()
            tr_running += loss.item() * xb.size(0)
            n_seen += xb.size(0)
        tr_loss = tr_running / n_seen

        model.eval()
        va_running, n_seen_va = 0.0, 0
        with torch.no_grad():
            for (xb,) in dl_va:
                xb = xb.to(device, non_blocking=True)
                x_hat = model(xb)
                va_running += composite_loss(x_hat, xb).item() * xb.size(0)
                n_seen_va += xb.size(0)
        va_loss = va_running / n_seen_va

        elapsed = time.time() - t0
        flag = ""
        if va_loss < best_val:
            best_val = va_loss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            flag = "  <- best"
        print(f"[train] epoch {epoch:2d}/{args.epochs}  train={tr_loss:.5f}  val={va_loss:.5f}  ({elapsed:.1f}s){flag}")

    assert best_state is not None
    model.load_state_dict(best_state)
    model.eval()

    val_scores = score_windows(model, X_train[va_idx], device)
    threshold = float(np.quantile(val_scores, args.threshold_quantile))
    print(f"[train] threshold (p{args.threshold_quantile * 100:g} on val scores): {threshold:.5f}")
    print(f"[train] val_scores: min={val_scores.min():.5f}  max={val_scores.max():.5f}  "
          f"mean={val_scores.mean():.5f}  median={np.median(val_scores):.5f}")

    # ONNX export (FP32 + FP16, FP16 is what Kusto uses).
    onnx_fp32 = art_dir / "model.onnx"
    onnx_fp16 = art_dir / "model.fp16.onnx"
    dummy = torch.randn(1, WINDOW, N_FEATURES, dtype=torch.float32)

    export_model = ScoreWrapper(copy.deepcopy(model).to("cpu").eval()).eval()
    torch.onnx.export(
        export_model, dummy, onnx_fp32.as_posix(),
        input_names=["window"], output_names=["score"],
        dynamic_axes={"window": {0: "batch"}, "score": {0: "batch"}},
        opset_version=17, do_constant_folding=True, dynamo=False,
    )
    mp32 = onnx.load(onnx_fp32.as_posix())
    if mp32.ir_version > SANDBOX_IR_VERSION:
        mp32.ir_version = SANDBOX_IR_VERSION
    onnx.save(mp32, onnx_fp32.as_posix())
    onnx.checker.check_model(mp32)

    fp16_model = ScoreWrapperFP16(copy.deepcopy(model).to("cpu").eval()).eval()
    torch.onnx.export(
        fp16_model, dummy, onnx_fp16.as_posix(),
        input_names=["window"], output_names=["score"],
        dynamic_axes={"window": {0: "batch"}, "score": {0: "batch"}},
        opset_version=17, do_constant_folding=True, dynamo=False,
    )
    mp16 = onnx.load(onnx_fp16.as_posix())
    if mp16.ir_version > SANDBOX_IR_VERSION:
        mp16.ir_version = SANDBOX_IR_VERSION
    onnx.save(mp16, onnx_fp16.as_posix())
    onnx.checker.check_model(mp16)

    def size_report(path: Path) -> dict:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw)
        return {
            "path": str(path.relative_to(REPO_ROOT)),
            "raw_kb": round(len(raw) / 1024, 1),
            "base64_kb": round(len(b64) / 1024, 1),
            "kusto_row_fits": len(b64) <= KUSTO_ROW_BUDGET_BYTES,
        }

    fp32_info = size_report(onnx_fp32)
    fp16_info = size_report(onnx_fp16)
    print(f"[train] FP32  raw={fp32_info['raw_kb']} KB  b64={fp32_info['base64_kb']} KB  fits={fp32_info['kusto_row_fits']}")
    print(f"[train] FP16  raw={fp16_info['raw_kb']} KB  b64={fp16_info['base64_kb']} KB  fits={fp16_info['kusto_row_fits']}")

    # ONNX parity sanity check on a few windows.
    sample = X_train[va_idx[:128]].astype(np.float32)
    with torch.no_grad():
        torch_scores = export_model(torch.from_numpy(sample)).numpy()
    ort_fp16 = ort.InferenceSession(onnx_fp16.as_posix(), providers=["CPUExecutionProvider"]).run(
        ["score"], {"window": sample})[0]
    rel = float(np.max(np.abs(ort_fp16 - torch_scores) / np.maximum(np.abs(torch_scores), 1e-6)))
    print(f"[train] FP16 ONNX vs PyTorch max |rel diff|: {rel:.2%}")

    # Scaler + metadata.
    (art_dir / "scaler.json").write_text(json.dumps({
        "sensors": SENSORS,
        "mean": [float(x) for x in mean],
        "std": [float(x) for x in std],
    }, indent=2))

    (art_dir / "metadata.json").write_text(json.dumps({
        "model": model_name,
        "machine_id": machine,
        "window": WINDOW,
        "stride_train": STRIDE_TRAIN,
        "stride_eval": STRIDE_EVAL,
        "n_features": N_FEATURES,
        "d_model": D_MODEL,
        "n_heads": N_HEADS,
        "n_enc_layers": N_ENC,
        "n_dec_layers": N_DEC,
        "ff_dim": FF_DIM,
        "n_parameters": n_params,
        "threshold": threshold,
        "threshold_rule": f"p{args.threshold_quantile * 100:g} on training-val window scores",
        "training": {
            "epochs": args.epochs,
            "batch": args.batch,
            "lr": args.lr,
            "weight_decay": WEIGHT_DECAY,
            "best_val_loss": best_val,
            "device": str(device),
        },
        "onnx": {
            "ir_version": SANDBOX_IR_VERSION,
            "fp32": fp32_info,
            "fp16": fp16_info,
            "parity_fp16_max_rel_diff": rel,
        },
        "scaler": {
            "sensors": SENSORS,
            "mean": [float(x) for x in mean],
            "std": [float(x) for x in std],
        },
        "kusto_deployable": fp16_info["kusto_row_fits"],
    }, indent=2, default=str))

    torch.save(model.state_dict(), art_dir / "model.pt")

    print(f"[train] artifacts in {art_dir.relative_to(REPO_ROOT)}:")
    for p in sorted(art_dir.iterdir()):
        print(f"  {p.name:>20s}   {p.stat().st_size / 1024:>8.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
