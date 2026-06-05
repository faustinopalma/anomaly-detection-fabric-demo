"""SOTA anomaly-detection lab for the 3-sensor CNC spindles M-002 and M-003.

This module builds a *labeled* evaluation set (scale-aware spike/drift/stuck
injections with window-level ground truth, mirroring the live simulator's
``AnomalyOverlay``), trains a small grid of Transformer-autoencoder variants
(plain reconstruction vs denoising/masked, a couple of right-sized capacities,
two score aggregations), selects the variant with the best F1 / PR-AUC on the
labeled eval, retrains the winner on all normal data, calibrates a best-F1
threshold on a fresh injected eval, and exports a Kusto-deployable single-file
FP16 ONNX.

Data comes from the *simulator code* (train/serve consistency):

* M-003 -> ``simulator-local/cnc_engine.generate_frame(data/cnc_profile_M-003.json)``
* M-002 -> the served replay trace ``simulator-cloud/src/synth_trace_M-002.json``

The KQL scorer (``kql/03_scoring_functions.kql``) reads ``window_size``,
``sensors``, the scaler and ``metadata.threshold`` from the model row and runs
the baked-in ONNX score, so deploying a new model is a pure
``tools/05_register_model.py`` re-register (version bump). No KQL or simulator
change is required; the model name key ``transformer_ae_small__M-00X`` is kept
as an opaque identifier (metadata records the true, larger architecture).

Usage
-----
    .venv/Scripts/python.exe tools/cnc_ae_lab.py M-003 --epochs 18
    .venv/Scripts/python.exe tools/cnc_ae_lab.py M-002 --epochs 18 --save
    .venv/Scripts/python.exe tools/cnc_ae_lab.py M-003 --quick        # tiny sweep
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "simulator-local"))
sys.path.insert(0, str(REPO / "tools"))

from cnc_engine import generate_frame, load_profile  # noqa: E402
from train_cnc_m003 import (  # noqa: E402  (reuse the proven, Kusto-safe export)
    KUSTO_ROW_BUDGET_BYTES,
    SANDBOX_IR_VERSION,
    export_onnx_single_file,
)

# ---------------------------------------------------------------------------
# Fixed pipeline constants (must match the KQL scorer's window contract).
# ---------------------------------------------------------------------------
WINDOW = 64
STRIDE_TRAIN = 16
STRIDE_EVAL = 1
SEED = 1337

# Right-sizing guard: keep FP16 base64 below the 1 MB Kusto row budget with
# margin. ~3.05 ONNX bytes / param empirically -> ~240k params is the ceiling.
MAX_PARAMS = 245_000

# Injection overlay constants — copied verbatim from
# simulator-cloud/src/simulate_machines.py so the labeled eval matches what the
# live simulator actually emits when an operator injects an anomaly.
BASE_DURATION = {"spike": 28.0, "drift": 22.0, "stuck": 20.0}
SPIKE_SIGMA_K = 1.6
DRIFT_SIGMA_K = 1.8
LEVEL_MAGNITUDE = {1: 0.6, 2: 0.85, 3: 1.0, 4: 1.35, 5: 1.8}
LEVEL_DURATION = {1: 0.6, 2: 0.8, 3: 1.0, 4: 1.3, 5: 1.7}

# A window counts as a positive (anomalous) detection target only when an
# injection covers a substantial fraction of it — a few stray injected samples
# at the window edge are genuinely ambiguous and excluded from the metric.
POS_COVERAGE = 16   # >= 16/64 injected samples -> positive
# windows with 1..15 injected samples are the ambiguous guard band (ignored).


# ===========================================================================
# Model
# ===========================================================================
@dataclass
class Config:
    name: str
    d_model: int = 64
    n_heads: int = 4
    n_enc: int = 2
    n_dec: int = 2
    ff_dim: int = 192
    dropout: float = 0.1
    denoise: bool = False
    noise_sigma: float = 0.15
    mask_frac: float = 0.12
    agg: str = "maxmean"          # "maxmean" | "meanmean"
    epochs: int = 18
    lr: float = 3e-4
    batch: int = 256
    weight_decay: float = 1e-5
    lam_delta: float = 0.1


class TransformerAE(nn.Module):
    """Learned-query Transformer autoencoder (same topology as the deployed
    model, parametrised so the sweep can scale capacity)."""

    def __init__(self, n_features: int, cfg: Config) -> None:
        super().__init__()
        d = cfg.d_model
        self.window = WINDOW
        self.input_proj = nn.Linear(n_features, d)
        self.output_head = nn.Linear(d, n_features)
        self.pos_enc = nn.Parameter(torch.randn(1, WINDOW, d) * 0.02)
        self.pos_dec = nn.Parameter(torch.randn(1, WINDOW, d) * 0.02)
        self.query = nn.Parameter(torch.randn(1, WINDOW, d) * 0.02)
        enc_layer = nn.TransformerEncoderLayer(
            d_model=d, nhead=cfg.n_heads, dim_feedforward=cfg.ff_dim,
            dropout=cfg.dropout, batch_first=True, activation="gelu", norm_first=True)
        self.encoder = nn.TransformerEncoder(enc_layer, num_layers=cfg.n_enc)
        dec_layer = nn.TransformerDecoderLayer(
            d_model=d, nhead=cfg.n_heads, dim_feedforward=cfg.ff_dim,
            dropout=cfg.dropout, batch_first=True, activation="gelu", norm_first=True)
        self.decoder = nn.TransformerDecoder(dec_layer, num_layers=cfg.n_dec)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.input_proj(x) + self.pos_enc
        memory = self.encoder(h)
        q = self.query.expand(x.shape[0], -1, -1) + self.pos_dec
        y = self.decoder(q, memory)
        return self.output_head(y)


def _aggregate(err: torch.Tensor, agg: str) -> torch.Tensor:
    """err: [B, T, S] squared error -> [B] window score."""
    per_sensor = err.mean(dim=1)                  # [B, S] mean over time
    if agg == "meanmean":
        return per_sensor.mean(dim=1)
    return per_sensor.max(dim=1).values           # "maxmean" (default)


class ScoreWrapper(nn.Module):
    """FP32 scorer baked for ONNX export. ``agg`` is a python constant captured
    at construction (constant-folded by the TorchScript exporter)."""

    def __init__(self, ae: nn.Module, agg: str) -> None:
        super().__init__()
        self.ae = ae
        self.agg = agg

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        err = (self.ae(x) - x) ** 2
        per_sensor = err.mean(dim=1)
        if self.agg == "meanmean":
            return per_sensor.mean(dim=1)
        return per_sensor.max(dim=1).values


class ScoreWrapperFP16(nn.Module):
    def __init__(self, ae: nn.Module, agg: str) -> None:
        super().__init__()
        self.ae = ae.half()
        self.agg = agg

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x16 = x.half()
        err = (self.ae(x16) - x16) ** 2
        per_sensor = err.mean(dim=1)
        if self.agg == "meanmean":
            out = per_sensor.mean(dim=1)
        else:
            out = per_sensor.max(dim=1).values
        return out.float()


def composite_loss(x_hat: torch.Tensor, x: torch.Tensor, lam_delta: float) -> torch.Tensor:
    mse = torch.mean((x_hat - x) ** 2)
    dx_hat = x_hat[:, 1:, :] - x_hat[:, :-1, :]
    dx = x[:, 1:, :] - x[:, :-1, :]
    l1d = torch.mean(torch.abs(dx_hat - dx))
    return mse + lam_delta * l1d


# ===========================================================================
# Data (from the simulator code)
# ===========================================================================
def load_active_series(machine: str, hours: float, seed: int) -> tuple[np.ndarray, list[str], dict]:
    """Return ``(active[N, S], sensors, units)`` raw 1 Hz active-only telemetry."""
    if machine == "M-003":
        profile = load_profile(REPO / "data" / "cnc_profile_M-003.json")
        sensors = list(profile["sensors"])
        units = profile.get("units", {})
        vals, mask = generate_frame(profile, int(hours * 3600), seed=seed)
        return vals[mask].astype(np.float32), sensors, units
    if machine == "M-002":
        # Train on the FULL synthgen trace (``_local/synthgen/synth_trace_full.npz``),
        # the same generator bundle the simulator replays — matching the proven
        # tools/train_m002_synth.py path for train/serve consistency. The 4h sim
        # slice shipped in the container is far too short (only ~600 windows);
        # the full 24h trace gives ~6x more data and a far tighter normal tail.
        # Fall back to the sim slice only if the full trace is unavailable.
        units = {"mandrino_load": "%", "mandrino_power": "kW",
                 "mandrino_torque": "N*cm"}
        npz_path = REPO / "_local" / "synthgen" / "synth_trace_full.npz"
        if npz_path.exists():
            data = np.load(npz_path, allow_pickle=True)
            sensors = [str(s) for s in data["sensors"].tolist()]
            vals = data["values"].astype(np.float32)
            active = data["active"].astype(bool)
            return vals[active].astype(np.float32), sensors, units
        trace_path = REPO / "simulator-cloud" / "src" / "synth_trace_M-002.json"
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        sensors = list(trace["sensors"])
        units = trace.get("units", units)
        vals = np.asarray(trace["values"], dtype=np.float32)
        active = np.asarray(trace.get("active", np.ones(len(vals))), dtype=bool)
        return vals[active].astype(np.float32), sensors, units
    raise ValueError(f"unsupported machine {machine!r}")


def fit_scaler(active: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = active.mean(axis=0).astype(np.float32)
    std = active.std(axis=0)
    std[std == 0] = 1.0
    return mean, std.astype(np.float32)


def build_windows(arr: np.ndarray, stride: int) -> np.ndarray:
    n, s = arr.shape
    if n < WINDOW:
        return np.empty((0, WINDOW, s), dtype=np.float32)
    out = np.lib.stride_tricks.sliding_window_view(arr, window_shape=WINDOW, axis=0)[::stride]
    return np.ascontiguousarray(out.transpose(0, 2, 1))


def inject_series(raw: np.ndarray, rng: np.random.Generator,
                  n_inj: int = 60, levels=(2, 3, 4),
                  kinds=("spike", "drift", "stuck")) -> tuple[np.ndarray, np.ndarray]:
    """Apply scale-aware spike/drift/stuck overlays to a copy of ``raw``.

    Returns ``(series_mod[N, S], inj_mask[N])``. Mirrors the formulas in
    ``simulator-cloud/src/simulate_machines.py::AnomalyOverlay.apply`` (local
    operating sigma from a trailing 120-sample buffer)."""
    T, S = raw.shape
    series = raw.copy()
    inj_mask = np.zeros(T, dtype=bool)
    cursor = WINDOW + 130
    placed = 0
    min_gap = WINDOW + 40
    while cursor < T - 64 and placed < n_inj:
        kind = str(rng.choice(kinds))
        level = int(rng.choice(levels))
        si = int(rng.integers(0, S))
        mag = LEVEL_MAGNITUDE[level]
        dur = max(6, int(round(BASE_DURATION[kind] * LEVEL_DURATION[level])))
        seg = slice(cursor, min(cursor + dur, T))
        seg_len = seg.stop - seg.start
        sigma = float(raw[max(0, cursor - 120):cursor, si].std())
        v = series[seg, si].astype(np.float64)
        if kind == "spike":
            amp = np.maximum.reduce([np.abs(v) * 0.5, np.full_like(v, 1.0),
                                     np.full_like(v, SPIKE_SIGMA_K * sigma)]) * (1.5 + 1.5 * mag)
            sign = np.where(v >= 0, 1.0, -1.0)
            jitter = rng.uniform(0.85, 1.15, size=seg_len)
            series[seg, si] = (v + sign * amp * jitter).astype(np.float32)
        elif kind == "drift":
            t_in = (np.arange(seg_len) + 1) / max(1, seg_len)
            amp = np.maximum.reduce([np.abs(v) * 0.4, np.full_like(v, 1.0),
                                     np.full_like(v, DRIFT_SIGMA_K * sigma)]) * (1.0 + mag)
            series[seg, si] = (v + t_in * amp).astype(np.float32)
        else:  # stuck
            v0 = float(v[0])
            sign = 1.0 if v0 >= 0 else -1.0
            stuck_value = v0 + sign * SPIKE_SIGMA_K * sigma * (1.0 + mag)
            series[seg, si] = np.float32(stuck_value)
        inj_mask[seg] = True
        cursor = seg.stop + min_gap
        placed += 1
    return series, inj_mask


def windows_with_labels(series_std: np.ndarray, inj_mask: np.ndarray
                        ) -> tuple[np.ndarray, np.ndarray]:
    """Build stride-1 windows and assign labels: 1=anomaly, 0=normal, -1=ignore."""
    X = build_windows(series_std, STRIDE_EVAL)
    n = X.shape[0]
    cov = np.empty(n, dtype=np.int32)
    cum = np.concatenate([[0], np.cumsum(inj_mask.astype(np.int32))])
    for i in range(n):
        cov[i] = cum[i + WINDOW] - cum[i]
    y = np.full(n, -1, dtype=np.int8)
    y[cov == 0] = 0
    y[cov >= POS_COVERAGE] = 1
    return X, y


# ===========================================================================
# Train / score / metrics
# ===========================================================================
def train_model(cfg: Config, X_tr: np.ndarray, X_va: np.ndarray,
                n_features: int, device: torch.device, verbose: bool = False
                ) -> tuple[nn.Module, float]:
    torch.manual_seed(SEED)
    model = TransformerAE(n_features, cfg).to(device)
    dl_tr = DataLoader(TensorDataset(torch.from_numpy(X_tr)), batch_size=cfg.batch,
                       shuffle=True, drop_last=True)
    dl_va = DataLoader(TensorDataset(torch.from_numpy(X_va)), batch_size=cfg.batch, shuffle=False)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=cfg.epochs * max(1, len(dl_tr)))
    best_val, best_state = float("inf"), None
    for epoch in range(1, cfg.epochs + 1):
        model.train()
        for (xb,) in dl_tr:
            xb = xb.to(device)
            target = xb
            inp = xb
            if cfg.denoise:
                if cfg.noise_sigma > 0:
                    inp = inp + cfg.noise_sigma * torch.randn_like(inp)
                if cfg.mask_frac > 0:
                    keep = (torch.rand(inp.shape[0], inp.shape[1], 1, device=device)
                            > cfg.mask_frac).float()
                    inp = inp * keep
            opt.zero_grad(set_to_none=True)
            loss = composite_loss(model(inp), target, cfg.lam_delta)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
        model.eval()
        va, n = 0.0, 0
        with torch.no_grad():
            for (xb,) in dl_va:
                xb = xb.to(device)
                va += composite_loss(model(xb), xb, cfg.lam_delta).item() * xb.size(0)
                n += xb.size(0)
        va /= max(1, n)
        if va < best_val:
            best_val = va
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        if verbose:
            print(f"      epoch {epoch:2d}/{cfg.epochs}  val={va:.5f}")
    assert best_state is not None
    model.load_state_dict(best_state)
    model.eval()
    return model, best_val


@torch.no_grad()
def score_windows(model: nn.Module, X: np.ndarray, agg: str, device: torch.device,
                  batch: int = 4096) -> np.ndarray:
    model.eval()
    out = np.empty(len(X), dtype=np.float32)
    for i in range(0, len(X), batch):
        xb = torch.from_numpy(X[i:i + batch]).to(device)
        err = (model(xb) - xb) ** 2
        out[i:i + batch] = _aggregate(err, agg).cpu().numpy()
    return out


def pr_metrics(scores: np.ndarray, y: np.ndarray) -> dict:
    """Precision/recall analysis over labeled (0/1) windows. Returns PR-AUC
    (average precision), best-F1 and the threshold achieving it."""
    keep = y >= 0
    s = scores[keep]
    lab = y[keep].astype(np.int32)
    n_pos = int(lab.sum())
    n_neg = int((lab == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return {"pr_auc": float("nan"), "best_f1": float("nan"), "best_thr": float("nan"),
                "precision": float("nan"), "recall": float("nan"),
                "n_pos": n_pos, "n_neg": n_neg}
    order = np.argsort(-s, kind="mergesort")
    s_sorted = s[order]
    lab_sorted = lab[order]
    tp = np.cumsum(lab_sorted)
    fp = np.cumsum(1 - lab_sorted)
    precision = tp / np.maximum(tp + fp, 1)
    recall = tp / n_pos
    # Average precision = sum of (recall_k - recall_{k-1}) * precision_k.
    rec_prev = np.concatenate([[0.0], recall[:-1]])
    ap = float(np.sum((recall - rec_prev) * precision))
    f1 = 2 * precision * recall / np.maximum(precision + recall, 1e-9)
    bi = int(np.argmax(f1))
    return {"pr_auc": ap, "best_f1": float(f1[bi]), "best_thr": float(s_sorted[bi]),
            "precision": float(precision[bi]), "recall": float(recall[bi]),
            "n_pos": n_pos, "n_neg": n_neg}


def f1_at_threshold(scores: np.ndarray, y: np.ndarray, thr: float) -> dict:
    keep = y >= 0
    s, lab = scores[keep], y[keep].astype(np.int32)
    pred = (s > thr).astype(np.int32)
    tp = int(((pred == 1) & (lab == 1)).sum())
    fp = int(((pred == 1) & (lab == 0)).sum())
    fn = int(((pred == 0) & (lab == 1)).sum())
    prec = tp / max(1, tp + fp)
    rec = tp / max(1, tp + fn)
    f1 = 2 * prec * rec / max(1e-9, prec + rec)
    return {"precision": prec, "recall": rec, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


# ===========================================================================
# Variant grid
# ===========================================================================
def default_grid(quick: bool, epochs: int) -> list[Config]:
    if quick:
        return [
            Config("base56", d_model=56, ff_dim=160, denoise=False, epochs=max(6, epochs // 2)),
            Config("denoise64", d_model=64, ff_dim=176, denoise=True, epochs=max(6, epochs // 2)),
        ]
    return [
        # baseline == current deployed "small" architecture (161k params)
        Config("base56", d_model=56, n_enc=2, n_dec=2, ff_dim=160, denoise=False, epochs=epochs),
        # right-sized plain reconstruction (~205k params)
        Config("recon64", d_model=64, n_enc=2, n_dec=2, ff_dim=176, denoise=False, epochs=epochs),
        # denoising at the current capacity (isolates the training-method gain)
        Config("denoise56", d_model=56, n_enc=2, n_dec=2, ff_dim=160, denoise=True, epochs=epochs),
        # right-sized denoising (the favourite, ~205k params)
        Config("denoise64", d_model=64, n_enc=2, n_dec=2, ff_dim=176, denoise=True, epochs=epochs),
        # deeper encoder, denoising (~234k params)
        Config("denoise64d", d_model=64, n_enc=3, n_dec=2, ff_dim=160, denoise=True, epochs=epochs),
        # right-sized denoising with the robust mean aggregation
        Config("denoise64m", d_model=64, n_enc=2, n_dec=2, ff_dim=176, denoise=True,
               agg="meanmean", epochs=epochs),
    ]


def count_params(cfg: Config, n_features: int) -> int:
    m = TransformerAE(n_features, cfg)
    return sum(p.numel() for p in m.parameters())


# ===========================================================================
# Orchestration
# ===========================================================================
def run(machine: str, hours: float, epochs: int, quick: bool, save: bool) -> int:
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    active, sensors, units = load_active_series(machine, hours, SEED)
    n_features = len(sensors)
    # M-002 replays a FINITE synthgen trace, so (unlike M-003's generator) we
    # cannot draw a fresh eval set: reserve a held-out tail that the final model
    # never trains on, and calibrate the threshold / final metrics there.
    finite_source = machine == "M-002"
    print(f"[data] {machine}: active samples={len(active):,}  sensors={sensors}  device={device}")

    if finite_source:
        ecut = int(len(active) * 0.82)
        model_raw, eval_segment = active[:ecut], active[ecut:]
        print(f"[data] finite source: model={len(model_raw):,}  held-out eval={len(eval_segment):,}")
    else:
        model_raw, eval_segment = active, None

    # Split: first 80% -> model fit; last 20% -> held-out for selection eval.
    cut = int(len(model_raw) * 0.80)
    fit_raw, hold_raw = model_raw[:cut], model_raw[cut:]
    mean, std = fit_scaler(fit_raw)

    def standardize(a: np.ndarray) -> np.ndarray:
        return ((a - mean) / std).astype(np.float32)

    X_all = build_windows(standardize(fit_raw), STRIDE_TRAIN)
    rng = np.random.default_rng(SEED)
    idx = rng.permutation(len(X_all))
    vcut = int(len(X_all) * 0.85)
    X_tr, X_va = X_all[idx[:vcut]], X_all[idx[vcut:]]
    print(f"[data] fit windows: train={len(X_tr)} val={len(X_va)}")

    # Selection eval (seed A) built from the held-out normal data.
    inj_a, mask_a = inject_series(hold_raw, np.random.default_rng(7), n_inj=80)
    Xe_a, ye_a = windows_with_labels(standardize(inj_a), mask_a)
    print(f"[eval] selection set: pos={int((ye_a==1).sum())} neg={int((ye_a==0).sum())} "
          f"ignore={int((ye_a==-1).sum())}")

    grid = default_grid(quick, epochs)
    results = []
    print(f"\n[sweep] {len(grid)} variants")
    print(f"  {'variant':<12} {'params':>8} {'fits':>5} {'val_loss':>9} "
          f"{'PR-AUC':>7} {'F1':>6} {'prec':>6} {'rec':>6}")
    for cfg in grid:
        npar = count_params(cfg, n_features)
        fits = npar <= MAX_PARAMS
        if not fits:
            print(f"  {cfg.name:<12} {npar:>8,} {'NO':>5}  (over budget, skipped)")
            continue
        t0 = time.time()
        model, vloss = train_model(cfg, X_tr, X_va, n_features, device)
        scores = score_windows(model, Xe_a, cfg.agg, device)
        m = pr_metrics(scores, ye_a)
        results.append((cfg, npar, vloss, m, model))
        print(f"  {cfg.name:<12} {npar:>8,} {'yes':>5} {vloss:>9.5f} "
              f"{m['pr_auc']:>7.3f} {m['best_f1']:>6.3f} {m['precision']:>6.3f} "
              f"{m['recall']:>6.3f}  ({time.time()-t0:.0f}s)")

    if not results:
        print("[error] no variant fit the size budget", file=sys.stderr)
        return 2

    # Select by F1 (tie-break PR-AUC).
    results.sort(key=lambda r: (r[3]["best_f1"], r[3]["pr_auc"]), reverse=True)
    win_cfg, win_par, win_vloss, win_m, _ = results[0]
    print(f"\n[select] winner: {win_cfg.name}  F1={win_m['best_f1']:.3f} "
          f"PR-AUC={win_m['pr_auc']:.3f}  params={win_par:,}  agg={win_cfg.agg}")

    # Retrain the winner on all NORMAL model data (excluding the held-out eval
    # segment for finite sources) for the final model.
    mean_f, std_f = fit_scaler(model_raw)

    def standardize_f(a: np.ndarray) -> np.ndarray:
        return ((a - mean_f) / std_f).astype(np.float32)

    Xf = build_windows(standardize_f(model_raw), STRIDE_TRAIN)
    fidx = np.random.default_rng(SEED + 1).permutation(len(Xf))
    fcut = int(len(Xf) * 0.9)
    final_model, final_vloss = train_model(win_cfg, Xf[fidx[:fcut]], Xf[fidx[fcut:]],
                                           n_features, device, verbose=False)

    # Unbiased injected eval for threshold + final metrics: a fresh generator
    # draw for M-003, the reserved held-out tail for the finite M-002 trace.
    if finite_source:
        eval_raw = eval_segment
    else:
        eval_raw, _, _ = load_active_series(machine, hours * 0.5, SEED + 99)
    inj_b, mask_b = inject_series(eval_raw, np.random.default_rng(101), n_inj=90)
    Xe_b, ye_b = windows_with_labels(standardize_f(inj_b), mask_b)
    scores_b = score_windows(final_model, Xe_b, win_cfg.agg, device)
    mb = pr_metrics(scores_b, ye_b)

    # Operating point = the F1-optimal threshold on the held-out injected eval
    # (the honest precision/recall trade-off), floored so that no more than ~3%
    # of truly-normal windows raise an alarm (precision guard; the activity gate
    # handles idle/OOD separately). On M-002's heavy-tailed synthgen normals the
    # unconstrained best-F1 point sits at ~5.3% FPR (too many per-window false
    # alarms); a p97 floor lands on the knee of the precision/recall curve
    # (~3% FPR, recall ~0.73, precision ~0.94) -- a far better balance than the
    # earlier 2% (p98) floor, which crushed recall to ~0.68. For M-003's clean
    # normals best-F1 already sits above the floor, so its threshold is unchanged.
    normal_scores = scores_b[ye_b == 0]
    p97 = float(np.quantile(normal_scores, 0.97))
    threshold = max(mb["best_thr"], p97)
    normal_fpr = float((normal_scores > threshold).mean())
    at = f1_at_threshold(scores_b, ye_b, threshold)
    print(f"\n[final] {win_cfg.name} retrained on all normal data (val={final_vloss:.5f})")
    print(f"[final] eval-B  PR-AUC={mb['pr_auc']:.3f}  best-F1={mb['best_f1']:.3f} "
          f"@thr={mb['best_thr']:.3f}  normal_p97={p97:.3f}")
    print(f"[final] chosen threshold={threshold:.4f} (normal FPR={normal_fpr:.2%}) -> "
          f"precision={at['precision']:.3f} recall={at['recall']:.3f} F1={at['f1']:.3f} "
          f"(tp={at['tp']} fp={at['fp']} fn={at['fn']})")

    if not save:
        print("\n[dry-run] pass --save to export + write models/ artifacts")
        return 0

    return _export(machine, final_model, win_cfg, mean_f, std_f, sensors, units,
                   threshold, mb, at, win_par, n_features, device)


def _export(machine, model, cfg, mean, std, sensors, units, threshold, mb, at,
            n_params, n_features, device) -> int:
    model_name = f"transformer_ae_small__{machine}"
    art_dir = REPO / "models" / model_name
    art_dir.mkdir(parents=True, exist_ok=True)
    onnx_fp32 = art_dir / "model.onnx"
    onnx_fp16 = art_dir / "model.fp16.onnx"
    dummy = torch.randn(1, WINDOW, n_features, dtype=torch.float32)

    export_model = ScoreWrapper(copy.deepcopy(model).to("cpu").eval(), cfg.agg).eval()
    export_onnx_single_file(export_model, dummy, onnx_fp32)
    fp16_model = ScoreWrapperFP16(copy.deepcopy(model).to("cpu").eval(), cfg.agg).eval()
    export_onnx_single_file(fp16_model, dummy, onnx_fp16)

    def size_report(path: Path) -> dict:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw)
        return {"path": path.name, "raw_kb": round(len(raw) / 1024, 1),
                "base64_kb": round(len(b64) / 1024, 1),
                "kusto_row_fits": len(b64) <= KUSTO_ROW_BUDGET_BYTES}

    fp32_info, fp16_info = size_report(onnx_fp32), size_report(onnx_fp16)
    print(f"[export] FP16 raw={fp16_info['raw_kb']} KB  b64={fp16_info['base64_kb']} KB  "
          f"fits={fp16_info['kusto_row_fits']}")
    if not fp16_info["kusto_row_fits"]:
        print("[error] FP16 ONNX exceeds the Kusto row budget — aborting export", file=sys.stderr)
        return 3

    # ONNX vs PyTorch parity on standardized samples.
    sample = build_windows(((load_active_series(machine, 4.0, SEED + 5)[0] - mean) / std)
                           .astype(np.float32), STRIDE_EVAL)[:128]
    with torch.no_grad():
        torch_scores = export_model(torch.from_numpy(sample)).numpy()
    ort_fp16 = ort.InferenceSession(onnx_fp16.as_posix(), providers=["CPUExecutionProvider"]).run(
        ["score"], {"window": sample})[0]
    rel = float(np.max(np.abs(ort_fp16 - torch_scores) / np.maximum(np.abs(torch_scores), 1e-6)))
    print(f"[export] FP16 ONNX vs PyTorch max |rel diff|: {rel:.2%}")

    scaler = {"sensors": sensors, "mean": [float(x) for x in mean], "std": [float(x) for x in std]}
    (art_dir / "scaler.json").write_text(json.dumps(scaler, indent=2))
    meta = {
        "model": model_name, "machine_id": machine, "window": WINDOW,
        "stride_train": STRIDE_TRAIN, "stride_eval": STRIDE_EVAL,
        "n_features": n_features, "d_model": cfg.d_model, "n_heads": cfg.n_heads,
        "n_enc_layers": cfg.n_enc, "n_dec_layers": cfg.n_dec, "ff_dim": cfg.ff_dim,
        "n_parameters": n_params, "threshold": threshold,
        "threshold_rule": "best-F1 on held-out injected eval, floored at normal p97 (<=3% FPR)",
        "architecture": f"transformer_ae denoising={cfg.denoise} agg={cfg.agg}",
        "variant": cfg.name,
        "data_source": ("real_cnc_profile (data/cnc_profile_M-003.json)" if machine == "M-003"
                        else "synthgen_full_trace (_local/synthgen/synth_trace_full.npz)"),
        "units": units,
        "training": {"epochs": cfg.epochs, "batch": cfg.batch, "lr": cfg.lr,
                     "weight_decay": cfg.weight_decay, "denoise": cfg.denoise,
                     "noise_sigma": cfg.noise_sigma, "mask_frac": cfg.mask_frac,
                     "device": str(device)},
        "eval": {"pr_auc": mb["pr_auc"], "best_f1": mb["best_f1"], "n_pos": mb["n_pos"],
                 "n_neg": mb["n_neg"], "precision_at_thr": at["precision"],
                 "recall_at_thr": at["recall"], "f1_at_thr": at["f1"]},
        "onnx": {"ir_version": SANDBOX_IR_VERSION, "fp32": fp32_info, "fp16": fp16_info,
                 "parity_fp16_max_rel_diff": rel},
        "scaler": scaler,
        "kusto_deployable": fp16_info["kusto_row_fits"],
    }
    (art_dir / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
    torch.save(model.state_dict(), art_dir / "model.pt")
    print(f"[export] wrote models/{model_name}/ (threshold={threshold:.4f}, "
          f"params={n_params:,}, variant={cfg.name})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("machine", choices=["M-002", "M-003"])
    ap.add_argument("--hours", type=float, default=120.0,
                    help="Hours of 1 Hz telemetry to generate for M-003 (ignored for M-002).")
    ap.add_argument("--epochs", type=int, default=18)
    ap.add_argument("--quick", action="store_true", help="Tiny 2-variant sweep for smoke tests.")
    ap.add_argument("--save", action="store_true", help="Export + write models/ artifacts.")
    args = ap.parse_args(argv)
    return run(args.machine, args.hours, args.epochs, args.quick, args.save)


if __name__ == "__main__":
    raise SystemExit(main())
