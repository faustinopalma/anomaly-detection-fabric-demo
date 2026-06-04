"""Fit the synthgen hybrid generator and emit a synthetic CNC trace for M-002.

This is the bridge between the ``synthgen/`` research package and the live
simulator. It

1. fits the hybrid generator (regime Markov + conditional diffusion + timing)
   on the real CNC telemetry (``_data_local/cnc_real_wide.parquet``),
2. generates a long, regular **1 Hz** trace of the three spindle signals plus a
   derived active/idle mask,
3. clamps every signal to the real observed per-signal range (a safety guard so
   an under-trained or heavy-tailed sample never injects absurd values into the
   live demo), and
4. persists two artifacts:

   * ``_local/synthgen/synth_trace_full.npz`` — the full trace (gitignored),
     consumed by ``tools/train_m002_synth.py`` to train the M-002 anomaly model
     on exactly what the simulator will replay (train/serve consistency);
   * ``simulator-cloud/src/synth_trace_M-002.json`` — a compact slice shipped in
     the container image and replayed (looped) by the ``SynthMachine``.

The generator is fitted once; both artifacts come from the same bundle, so the
distribution the inference model learns is identical to the one served live.

Usage
-----
    .venv/Scripts/python.exe tools/build_synth_trace.py
    .venv/Scripts/python.exe tools/build_synth_trace.py --epochs 80 \
        --subset-days 7 --train-hours 24 --sim-hours 4
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from synthgen.config import SIGNALS, load_config  # noqa: E402
from synthgen.data import build_dataset  # noqa: E402
from synthgen.pipeline import SynthBundle  # noqa: E402
from synthgen.pipeline import fit as fit_pipeline  # noqa: E402

MACHINE = "M-002"
GRID_PER_SECOND = 5  # synthgen grid is 200 ms; take every 5th step -> 1 Hz
UNITS = {"mandrino_load": "%", "mandrino_power": "kW", "mandrino_torque": "N*cm"}


def _real_ranges(wide_path: Path) -> dict[str, tuple[float, float]]:
    """Per-signal (min, max) from the real data, used to clamp synthetic output."""
    df = pd.read_parquet(wide_path)
    return {s: (float(df[s].min()), float(df[s].max())) for s in SIGNALS}


def _real_idle(real: pd.DataFrame, threshold: float) -> tuple[float, np.ndarray]:
    """Real duty cycle and per-signal idle noise std (samples below threshold)."""
    idle = real["mandrino_load"].abs().to_numpy() <= threshold
    duty = float((~idle).mean())
    if idle.any():
        idle_std = real.loc[idle, list(SIGNALS)].std().to_numpy(dtype=np.float32)
    else:
        idle_std = np.array([0.4, 0.3, 60.0], dtype=np.float32)
    idle_std = np.where(np.isfinite(idle_std) & (idle_std > 0), idle_std, 1.0)
    return duty, idle_std.astype(np.float32)


def _generate_signals(bundle: SynthBundle, n_steps: int, seed: int) -> np.ndarray:
    """Generate ``n_steps`` grid samples of the signals only.

    Mirrors ``synthgen.pipeline.generate`` but skips the timing point-process
    (its per-step Python loop dominates runtime and we only need a regular grid
    here — timestamps are discarded by the 1 Hz downsample anyway).
    """
    cfg = bundle.cfg
    win = cfg.data.window
    regime_seq = bundle.regime.sample(n_steps, seed=seed)
    n_blocks = int(np.ceil(n_steps / win))
    block_regime = np.array(
        [int(regime_seq[min(b * win, n_steps - 1)]) for b in range(n_blocks)],
        dtype=int,
    )
    norm_windows = bundle.diffusion.sample(block_regime, seed=seed)  # [B, L, C]
    sig_norm = norm_windows.reshape(-1, norm_windows.shape[2])[:n_steps]
    return bundle.scaler.inverse_transform(sig_norm).astype(np.float32)


def _fidelity(real: pd.DataFrame, synth_active: np.ndarray) -> None:
    print("\n[fidelity] active-sample marginal stats (real vs synth):")
    print(f"  {'signal':<16} {'real mean':>10} {'synth mean':>11} "
          f"{'real std':>10} {'synth std':>10} {'real rng':>20} {'synth rng':>20}")
    for i, s in enumerate(SIGNALS):
        rv = real[s].to_numpy()
        rv = rv[np.abs(rv) > 2.0]  # crude active filter on the real load proxy
        sv = synth_active[:, i]
        print(f"  {s:<16} {rv.mean():>10.2f} {sv.mean():>11.2f} "
              f"{rv.std():>10.2f} {sv.std():>10.2f} "
              f"{f'[{rv.min():.0f},{rv.max():.0f}]':>20} "
              f"{f'[{sv.min():.0f},{sv.max():.0f}]':>20}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=80,
                    help="Diffusion training epochs (CPU). Raise for fidelity.")
    ap.add_argument("--subset-days", type=float, default=7.0,
                    help="Days of real data to fit on (None-like 0 = all).")
    ap.add_argument("--train-hours", type=float, default=24.0,
                    help="Hours of 1 Hz trace to generate for inference training.")
    ap.add_argument("--sim-hours", type=float, default=4.0,
                    help="Hours of 1 Hz trace shipped to the simulator (looped).")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--reuse-bundle", action="store_true",
                    help="Skip fitting and reuse the bundle saved under the "
                         "synthgen out path (regime/diffusion/timing/scaler).")
    args = ap.parse_args(argv)

    cfg = load_config("local")
    cfg.subset_days = None if args.subset_days <= 0 else args.subset_days
    cfg.diffusion.epochs = args.epochs
    cfg.device = "cpu"

    wide_path = cfg.resolve(cfg.data.wide_path)
    ranges = _real_ranges(wide_path)
    real_df = pd.read_parquet(wide_path)
    threshold = cfg.data.active_load_threshold
    real_duty, idle_std = _real_idle(real_df, threshold)
    print(f"[data] real ranges: "
          + ", ".join(f"{s}=[{lo:.0f},{hi:.0f}]" for s, (lo, hi) in ranges.items()))
    print(f"[data] real duty={real_duty:.1%} (|load|>{threshold})  "
          f"idle_std={np.round(idle_std, 2).tolist()}")

    # 1) Fit the generator (persists the bundle under cfg.out_path) or reuse a
    #    previously-fitted bundle for fast iteration.
    if args.reuse_bundle:
        bundle = SynthBundle.load(cfg, cfg.out_path, device=cfg.device)
        print(f"[fit] reused saved bundle from {cfg.out_path}")
    else:
        ds = build_dataset(cfg)
        print(f"[fit] dataset windows={ds.n_windows} (subset_days={cfg.subset_days}, "
              f"epochs={cfg.diffusion.epochs})")
        bundle = fit_pipeline(cfg, ds, save=True)

    # 2) Generate a long 1 Hz trace (grid is 200 ms -> downsample by 5).
    n_grid = int(args.train_hours * 3600 * GRID_PER_SECOND)
    sig = _generate_signals(bundle, n_grid, args.seed)[::GRID_PER_SECOND]

    # 3) Clamp to the real observed range per signal (safety guard).
    for i, s in enumerate(SIGNALS):
        lo, hi = ranges[s]
        sig[:, i] = np.clip(sig[:, i], lo, hi)

    # Active mask from the spindle load (this machine runs nearly continuously:
    # real duty ~96 %). Rare idle samples are pinned to near-zero with the real
    # idle noise std so the live trace mirrors the real on/off behaviour.
    load_i = SIGNALS.index("mandrino_load")
    active = np.abs(sig[:, load_i]) > threshold
    rng = np.random.default_rng(args.seed)
    idle_noise = rng.normal(0.0, idle_std, size=sig.shape).astype(np.float32)
    sig = np.where(active[:, None], sig, idle_noise)

    duty = float(active.mean())
    print(f"[gen] trace: {len(sig):,} 1 Hz steps, duty={duty:.1%} (real {real_duty:.1%})")
    _fidelity(real_df, sig[active])

    # 4a) Full trace for inference training (gitignored).
    out_full = cfg.out_path / "synth_trace_full.npz"
    np.savez_compressed(out_full, values=sig, active=active,
                        sensors=np.array(list(SIGNALS)))
    print(f"[save] full training trace -> {out_full}")

    # 4b) Compact slice shipped in the simulator image.
    n_sim = int(args.sim_hours * 3600)
    sim_vals = np.round(sig[:n_sim], 4)
    sim_active = active[:n_sim]
    trace = {
        "machine_id": MACHINE,
        "kind": "synthgen_cnc_spindle",
        "description": (
            "Synthetic CNC spindle telemetry generated by the synthgen hybrid "
            "model (regime Markov + conditional diffusion + timing) fitted on "
            "the real customer telemetry. No real data is shipped; this is a "
            "fully synthetic, privacy-safe replay trace."),
        "source": "synthgen",
        "sample_rate_hz": 1.0,
        "sensors": list(SIGNALS),
        "units": UNITS,
        "duty_cycle": round(duty, 4),
        "values": sim_vals.tolist(),
        "active": sim_active.astype(int).tolist(),
    }
    out_sim = REPO / "simulator-cloud" / "src" / f"synth_trace_{MACHINE}.json"
    out_sim.write_text(json.dumps(trace))
    kb = out_sim.stat().st_size / 1024
    print(f"[save] simulator trace -> {out_sim}  ({kb:.0f} KB, {n_sim:,} steps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
