"""Self-contained cloud training: generate synthetic 8-sensor telemetry with
the repo's physics simulator, then train one ``transformer_ae_small`` model
per machine and export FP16 ONNX (Kusto-deployable).

Runs as an Azure ML command job. No external data dependency: the wide
training frame is produced in-process from the FSM + physics model that is a
faithful port of ``simulator-local/simulate_machines.py`` (notebook 01).

Artifacts are written under ``${AZ_OUTPUT}/transformer_ae_small__<MACHINE>/``
(the job maps this to ``./outputs`` so they can be downloaded afterwards).

Usage (inside the job):
    python generate_and_train.py --machines M-001 M-002 --hours 8 --epochs 12
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

# ---------------------------------------------------------------------------
# Sensor / model configuration (identical to tools/train_per_machine.py).
# ---------------------------------------------------------------------------
SENSORS = [
    "vibration_axial", "vibration_radial",
    "temperature_motor", "temperature_bearing",
    "pressure_hydraulic", "current", "power", "spindle_rpm",
]
N_FEATURES = len(SENSORS)

WINDOW = 64
STRIDE_TRAIN = 16
STRIDE_EVAL = 1
D_MODEL = 56
N_HEADS = 4
N_ENC = 2
N_DEC = 2
FF_DIM = 160
DROPOUT = 0.1

BATCH = 256
EPOCHS = 12
LR = 3e-4
WEIGHT_DECAY = 1e-5
SEED = 1337

SANDBOX_IR_VERSION = 9
KUSTO_ROW_BUDGET_BYTES = 1_048_576


# ---------------------------------------------------------------------------
# Physics simulator (port of simulator-local/simulate_machines.py).
# ---------------------------------------------------------------------------
class State(str):
    OFF = "OFF"
    STARTUP = "STARTUP"
    IDLE = "IDLE"
    PRODUCTION_LIGHT = "PRODUCTION_LIGHT"
    PRODUCTION_HEAVY = "PRODUCTION_HEAVY"
    RAMP_UP = "RAMP_UP"
    RAMP_DOWN = "RAMP_DOWN"
    SHUTDOWN = "SHUTDOWN"


@dataclass
class StateSpec:
    target_load: float
    dwell_s: tuple
    transitions: list


STATE_SPECS = {
    State.OFF: StateSpec(0.0, (120, 600), [(State.STARTUP, 1.0)]),
    State.STARTUP: StateSpec(0.10, (15, 30), [(State.IDLE, 1.0)]),
    State.IDLE: StateSpec(0.10, (60, 300), [
        (State.RAMP_UP, 0.7), (State.SHUTDOWN, 0.1), (State.IDLE, 0.2)]),
    State.RAMP_UP: StateSpec(0.5, (10, 30), [
        (State.PRODUCTION_LIGHT, 0.5), (State.PRODUCTION_HEAVY, 0.5)]),
    State.PRODUCTION_LIGHT: StateSpec(0.40, (180, 900), [
        (State.RAMP_UP, 0.3), (State.RAMP_DOWN, 0.4), (State.PRODUCTION_LIGHT, 0.3)]),
    State.PRODUCTION_HEAVY: StateSpec(0.85, (120, 600), [
        (State.RAMP_DOWN, 0.6), (State.PRODUCTION_HEAVY, 0.4)]),
    State.RAMP_DOWN: StateSpec(0.20, (10, 30), [
        (State.IDLE, 0.6), (State.PRODUCTION_LIGHT, 0.4)]),
    State.SHUTDOWN: StateSpec(0.0, (15, 40), [(State.OFF, 1.0)]),
}


def pick_next_state(current: str) -> str:
    spec = STATE_SPECS[current]
    states, weights = zip(*spec.transitions)
    w = np.array(weights, dtype=float)
    w = w / w.sum()
    idx = int(np.random.choice(len(states), p=w))
    return states[idx]


def pick_dwell(state: str) -> float:
    lo, hi = STATE_SPECS[state].dwell_s
    return random.uniform(lo, hi)


@dataclass
class Machine:
    machine_id: str
    nominal_rpm: float = 3000.0
    ambient_c: float = 22.0
    state: str = State.OFF
    state_elapsed_s: float = 0.0
    state_dwell_s: float = field(default_factory=lambda: pick_dwell(State.OFF))
    load_actual: float = 0.0
    T_motor: float = 22.0
    T_bearing: float = 22.0
    tau_load_ramp: float = 5.0
    tau_load_steady: float = 30.0
    tau_T_motor: float = 180.0
    tau_T_bearing: float = 300.0
    k_current_a: float = 1.0
    k_current_b: float = 14.0
    k_power_factor: float = 0.42
    k_pressure_a: float = 80.0
    k_pressure_b: float = 70.0
    k_vib_axial_base: float = 0.10
    k_vib_axial_load: float = 0.25
    k_vib_radial_base: float = 0.15
    k_vib_radial_load: float = 0.35
    k_rpm_droop: float = 0.05

    def step(self, dt: float) -> None:
        self.state_elapsed_s += dt
        if self.state_elapsed_s >= self.state_dwell_s:
            self.state = pick_next_state(self.state)
            self.state_elapsed_s = 0.0
            self.state_dwell_s = pick_dwell(self.state)

        target = STATE_SPECS[self.state].target_load
        tau = self.tau_load_ramp if self.state in (
            State.RAMP_UP, State.RAMP_DOWN, State.STARTUP, State.SHUTDOWN
        ) else self.tau_load_steady
        alpha = 1.0 - math.exp(-dt / tau)
        self.load_actual += alpha * (target - self.load_actual)

        heat_input = 0.0 if self.state == State.OFF else 60.0 * (self.load_actual ** 2)
        T_target_motor = self.ambient_c + heat_input
        a_m = 1.0 - math.exp(-dt / self.tau_T_motor)
        self.T_motor += a_m * (T_target_motor - self.T_motor)

        T_target_bearing = self.ambient_c + 0.85 * (self.T_motor - self.ambient_c)
        a_b = 1.0 - math.exp(-dt / self.tau_T_bearing)
        self.T_bearing += a_b * (T_target_bearing - self.T_bearing)

    def sample(self) -> dict:
        if self.state == State.OFF:
            return {k: 0.0 for k in SENSORS}
        load = max(0.0, self.load_actual)
        jitter_axial = float(np.random.normal(0, 0.02 + 0.05 * load))
        jitter_radial = float(np.random.normal(0, 0.03 + 0.07 * load))
        rpm = self.nominal_rpm * (1.0 - self.k_rpm_droop * load) + float(np.random.normal(0, 8))
        current = self.k_current_a + self.k_current_b * load + float(np.random.normal(0, 0.3))
        power = self.k_power_factor * current * (1.0 + 0.1 * load) + float(np.random.normal(0, 0.2))
        pressure = self.k_pressure_a + self.k_pressure_b * load + float(np.random.normal(0, 1.0))
        vib_a = self.k_vib_axial_base + self.k_vib_axial_load * load ** 1.5 + jitter_axial
        vib_r = self.k_vib_radial_base + self.k_vib_radial_load * load ** 1.2 + jitter_radial
        return {
            "temperature_motor": self.T_motor + float(np.random.normal(0, 0.4)),
            "temperature_bearing": self.T_bearing + float(np.random.normal(0, 0.3)),
            "vibration_axial": max(0.0, vib_a),
            "vibration_radial": max(0.0, vib_r),
            "current": max(0.0, current),
            "spindle_rpm": max(0.0, rpm),
            "pressure_hydraulic": max(0.0, pressure),
            "power": max(0.0, power),
        }


def generate_wide(machine_id: str, hours: float, seed: int) -> pd.DataFrame:
    """Generate a wide telemetry frame for one machine at 1 Hz."""
    random.seed(seed)
    np.random.seed(seed)
    n_ticks = int(hours * 3600)
    m = Machine(machine_id=machine_id,
                nominal_rpm=3000.0 * random.uniform(0.98, 1.02))
    start = datetime(2026, 5, 15, 8, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n_ticks):
        m.step(1.0)
        s = m.sample()
        rows.append({
            "ts": start + timedelta(seconds=i),
            "machineId": machine_id,
            "state": m.state,
            "load": m.load_actual,
            **s,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Model + training (identical to tools/train_per_machine.py).
# ---------------------------------------------------------------------------
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


def train_one(machine: str, df: pd.DataFrame, out_root: Path,
              epochs: int, batch: int, lr: float, threshold_quantile: float) -> dict:
    model_name = f"transformer_ae_small__{machine}"
    art_dir = out_root / model_name
    art_dir.mkdir(parents=True, exist_ok=True)

    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[train] machine={machine}  device={device}")
    if device.type == "cuda":
        print(f"[train] gpu={torch.cuda.get_device_name(0)}")

    df = df[df["machineId"] == machine]
    df = df[df["state"] != "OFF"].sort_values(["machineId", "ts"]).reset_index(drop=True)
    print(f"[train] rows after drop OFF: {len(df):,}")

    mean = df[SENSORS].mean(axis=0).astype(np.float32).values
    std = df[SENSORS].std(axis=0).replace(0, 1.0).astype(np.float32).values
    arr = ((df[SENSORS].to_numpy(dtype=np.float32) - mean) / std).astype(np.float32)

    X_train = build_windows(arr, STRIDE_TRAIN)
    print(f"[train] training windows : {X_train.shape}")

    tr_idx, va_idx = split_train_val(X_train)
    X_tr = torch.from_numpy(X_train[tr_idx])
    X_va = torch.from_numpy(X_train[va_idx])

    dl_tr = DataLoader(TensorDataset(X_tr), batch_size=batch, shuffle=True,
                       pin_memory=(device.type == "cuda"), drop_last=True)
    dl_va = DataLoader(TensorDataset(X_va), batch_size=batch, shuffle=False,
                       pin_memory=(device.type == "cuda"))

    model = TransformerAE().to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"[train] parameters: {n_params:,}")

    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=WEIGHT_DECAY)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs * max(1, len(dl_tr)))

    best_val = float("inf")
    best_state = None
    for epoch in range(1, epochs + 1):
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
        print(f"[train] epoch {epoch:2d}/{epochs}  train={tr_loss:.5f}  val={va_loss:.5f}  ({elapsed:.1f}s){flag}")

    assert best_state is not None
    model.load_state_dict(best_state)
    model.eval()

    val_scores = score_windows(model, X_train[va_idx], device)
    threshold = float(np.quantile(val_scores, threshold_quantile))
    print(f"[train] threshold (p{threshold_quantile * 100:g}): {threshold:.5f}")

    onnx_fp32 = art_dir / "model.onnx"
    onnx_fp16 = art_dir / "model.fp16.onnx"
    dummy = torch.randn(1, WINDOW, N_FEATURES, dtype=torch.float32)

    export_model = ScoreWrapper(copy.deepcopy(model).to("cpu").eval()).eval()
    torch.onnx.export(
        export_model, dummy, onnx_fp32.as_posix(),
        input_names=["window"], output_names=["score"],
        dynamic_axes={"window": {0: "batch"}, "score": {0: "batch"}},
        opset_version=17, do_constant_folding=True)
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
        opset_version=17, do_constant_folding=True)
    mp16 = onnx.load(onnx_fp16.as_posix())
    if mp16.ir_version > SANDBOX_IR_VERSION:
        mp16.ir_version = SANDBOX_IR_VERSION
    onnx.save(mp16, onnx_fp16.as_posix())
    onnx.checker.check_model(mp16)

    def size_report(path: Path) -> dict:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw)
        return {
            "path": path.name,
            "raw_kb": round(len(raw) / 1024, 1),
            "base64_kb": round(len(b64) / 1024, 1),
            "kusto_row_fits": len(b64) <= KUSTO_ROW_BUDGET_BYTES,
        }

    fp32_info = size_report(onnx_fp32)
    fp16_info = size_report(onnx_fp16)
    print(f"[train] FP16  raw={fp16_info['raw_kb']} KB  b64={fp16_info['base64_kb']} KB  fits={fp16_info['kusto_row_fits']}")

    sample = X_train[va_idx[:128]].astype(np.float32)
    with torch.no_grad():
        torch_scores = export_model(torch.from_numpy(sample)).numpy()
    ort_fp16 = ort.InferenceSession(onnx_fp16.as_posix(), providers=["CPUExecutionProvider"]).run(
        ["score"], {"window": sample})[0]
    rel = float(np.max(np.abs(ort_fp16 - torch_scores) / np.maximum(np.abs(torch_scores), 1e-6)))
    print(f"[train] FP16 ONNX vs PyTorch max |rel diff|: {rel:.2%}")

    (art_dir / "scaler.json").write_text(json.dumps({
        "sensors": SENSORS,
        "mean": [float(x) for x in mean],
        "std": [float(x) for x in std],
    }, indent=2))

    meta = {
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
        "threshold_rule": f"p{threshold_quantile * 100:g} on training-val window scores",
        "data_source": "synthetic_physics_simulator",
        "training": {
            "epochs": epochs, "batch": batch, "lr": lr,
            "weight_decay": WEIGHT_DECAY, "best_val_loss": best_val,
            "device": str(device),
        },
        "onnx": {
            "ir_version": SANDBOX_IR_VERSION,
            "fp32": fp32_info, "fp16": fp16_info,
            "parity_fp16_max_rel_diff": rel,
        },
        "scaler": {
            "sensors": SENSORS,
            "mean": [float(x) for x in mean],
            "std": [float(x) for x in std],
        },
        "kusto_deployable": fp16_info["kusto_row_fits"],
    }
    (art_dir / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    torch.save(model.state_dict(), art_dir / "model.pt")

    print(f"[train] artifacts in {art_dir.name}:")
    for p in sorted(art_dir.iterdir()):
        print(f"  {p.name:>20s}   {p.stat().st_size / 1024:>8.1f} KB")
    return meta


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--machines", nargs="+", default=["M-001", "M-002"])
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--epochs", type=int, default=EPOCHS)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--lr", type=float, default=LR)
    ap.add_argument("--threshold-quantile", type=float, default=0.995)
    args = ap.parse_args(argv)

    out_root = Path(os.environ.get("AZUREML_OUTPUT_DIR", "outputs"))
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"[main] torch={torch.__version__}  cuda_available={torch.cuda.is_available()}")
    print(f"[main] output root: {out_root.resolve()}")

    summary = {}
    for i, machine in enumerate(args.machines):
        print(f"\n===== generating + training {machine} =====")
        df = generate_wide(machine, hours=args.hours, seed=SEED + i)
        non_off = int((df["state"] != "OFF").sum())
        print(f"[gen] {machine}: {len(df):,} ticks, {non_off:,} non-OFF")
        meta = train_one(machine, df, out_root,
                         epochs=args.epochs, batch=args.batch, lr=args.lr,
                         threshold_quantile=args.threshold_quantile)
        summary[machine] = {
            "threshold": meta["threshold"],
            "fp16_b64_kb": meta["onnx"]["fp16"]["base64_kb"],
            "kusto_deployable": meta["kusto_deployable"],
            "best_val_loss": meta["training"]["best_val_loss"],
        }

    (out_root / "training_summary.json").write_text(json.dumps(summary, indent=2))
    print("\n[main] SUMMARY")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
