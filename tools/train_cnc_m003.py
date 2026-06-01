"""Train the M-003 (CNC spindle) anomaly model locally and export FP16 ONNX.

M-003 has only 3 sensors (mandrino_load, mandrino_power, mandrino_torque), so
the model is tiny and trains in seconds on CPU - no Azure ML node cold-start
needed (see notebooks/08_cloud_train_aml.ipynb for the cloud path and the
timing rationale). Training data comes from the shared CNC engine
(simulator-local/cnc_engine.py) driven by the empirical profile
data/cnc_profile_M-003.json, guaranteeing train/serve consistency with the
live simulator.

The architecture, ONNX export and baked-in score are identical to
cloud-training/src/generate_and_train.py (TransformerAE + ScoreWrapper), only
N_FEATURES differs (3 instead of 8).

Usage:
    .venv/Scripts/python.exe tools/train_cnc_m003.py
    .venv/Scripts/python.exe tools/train_cnc_m003.py --hours 40 --epochs 16
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
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "simulator-local"))
from cnc_engine import generate_frame, load_profile  # noqa: E402

PROFILE_PATH = REPO / "data" / "cnc_profile_M-003.json"
MACHINE = "M-003"
MODEL_NAME = f"transformer_ae_small__{MACHINE}"

WINDOW = 64
STRIDE_TRAIN = 16
STRIDE_EVAL = 1
D_MODEL = 56
N_HEADS = 4
N_ENC = 2
N_DEC = 2
FF_DIM = 160
DROPOUT = 0.1
WEIGHT_DECAY = 1e-5
SEED = 1337
SANDBOX_IR_VERSION = 9
KUSTO_ROW_BUDGET_BYTES = 1_048_576


class TransformerAE(nn.Module):
    def __init__(self, n_features: int) -> None:
        super().__init__()
        self.window = WINDOW
        self.input_proj = nn.Linear(n_features, D_MODEL)
        self.output_head = nn.Linear(D_MODEL, n_features)
        self.pos_enc = nn.Parameter(torch.randn(1, WINDOW, D_MODEL) * 0.02)
        self.pos_dec = nn.Parameter(torch.randn(1, WINDOW, D_MODEL) * 0.02)
        self.query = nn.Parameter(torch.randn(1, WINDOW, D_MODEL) * 0.02)
        enc_layer = nn.TransformerEncoderLayer(
            d_model=D_MODEL, nhead=N_HEADS, dim_feedforward=FF_DIM,
            dropout=DROPOUT, batch_first=True, activation="gelu", norm_first=True)
        self.encoder = nn.TransformerEncoder(enc_layer, num_layers=N_ENC)
        dec_layer = nn.TransformerDecoderLayer(
            d_model=D_MODEL, nhead=N_HEADS, dim_feedforward=FF_DIM,
            dropout=DROPOUT, batch_first=True, activation="gelu", norm_first=True)
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


def build_windows(arr: np.ndarray, stride: int, n_features: int) -> np.ndarray:
    n = arr.shape[0]
    if n < WINDOW:
        return np.empty((0, WINDOW, n_features), dtype=np.float32)
    out = np.lib.stride_tricks.sliding_window_view(arr, window_shape=WINDOW, axis=0)[::stride]
    return np.ascontiguousarray(out.transpose(0, 2, 1))


@torch.no_grad()
def score_windows(model: nn.Module, X: np.ndarray, device: torch.device, batch: int = 2048) -> np.ndarray:
    model.eval()
    out = np.empty(len(X), dtype=np.float32)
    for i in range(0, len(X), batch):
        xb = torch.from_numpy(X[i:i + batch]).to(device)
        x_hat = model(xb)
        err = (x_hat - xb) ** 2
        out[i:i + batch] = err.mean(dim=1).max(dim=1).values.cpu().numpy()
    return out


def export_onnx_single_file(module: nn.Module, dummy: torch.Tensor, path: Path) -> None:
    """Export to a single self-contained ONNX file (no external .data sidecar).

    The TorchScript exporter (``dynamo=False``) writes weights inline. Newer
    torch defaults to the dynamo exporter, which externalises tensors into a
    ``<name>.onnx.data`` sidecar - that breaks Kusto deployment (we base64 a
    single file). We force the legacy path and, as a belt-and-braces step,
    re-load with external data and re-save inlined, then drop any sidecar.
    """
    try:
        torch.onnx.export(
            module, dummy, path.as_posix(),
            input_names=["window"], output_names=["score"],
            dynamic_axes={"window": {0: "batch"}, "score": {0: "batch"}},
            opset_version=17, do_constant_folding=True, dynamo=False)
    except TypeError:
        torch.onnx.export(
            module, dummy, path.as_posix(),
            input_names=["window"], output_names=["score"],
            dynamic_axes={"window": {0: "batch"}, "score": {0: "batch"}},
            opset_version=17, do_constant_folding=True)

    m = onnx.load(path.as_posix(), load_external_data=True)
    if m.ir_version > SANDBOX_IR_VERSION:
        m.ir_version = SANDBOX_IR_VERSION
    # Inline any externally-stored initializers.
    for t in m.graph.initializer:
        if t.data_location == onnx.TensorProto.EXTERNAL:
            t.ClearField("external_data")
            t.data_location = onnx.TensorProto.DEFAULT
    onnx.save_model(m, path.as_posix(), save_as_external_data=False)
    sidecar = Path(path.as_posix() + ".data")
    if sidecar.exists():
        sidecar.unlink()
    onnx.checker.check_model(onnx.load(path.as_posix()))



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=40.0,
                    help="Hours of 1 Hz telemetry to generate (default 40 -> ~4 h active).")
    ap.add_argument("--epochs", type=int, default=16)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--threshold-quantile", type=float, default=0.995)
    args = ap.parse_args(argv)

    profile = load_profile(PROFILE_PATH)
    sensors = profile["sensors"]
    n_features = len(sensors)
    np.random.seed(SEED)
    torch.manual_seed(SEED)

    n_seconds = int(args.hours * 3600)
    print(f"[gen] generating {args.hours:g} h ({n_seconds:,} ticks) of CNC telemetry...")
    vals, mask = generate_frame(profile, n_seconds, seed=SEED)
    active = vals[mask]
    print(f"[gen] active samples: {len(active):,} / {n_seconds:,} ({mask.mean():.1%} duty)")

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
        "data_source": "real_cnc_profile (data/cnc_profile_M-003.json)",
        "units": profile["units"],
        "training": {"epochs": args.epochs, "batch": args.batch, "lr": args.lr,
                     "weight_decay": WEIGHT_DECAY, "best_val_loss": best_val,
                     "device": str(device)},
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
