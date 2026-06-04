"""Azure ML entrypoint — trains the synthgen hybrid generator on GPU.

This script is the command the AML job runs. It is intentionally thin: it loads
the *cloud* configuration, builds the dataset from the data staged in the job
snapshot, fits the full pipeline (scaler + regime + timing + diffusion), logs
metrics to MLflow, and writes all artifacts to ``outputs/`` so they can be
downloaded back to the repo.

The same ``synthgen`` package is used locally and here — only the config differs.
The package is expected to live alongside this file in the uploaded snapshot;
we prepend the snapshot root to ``sys.path`` so ``import synthgen`` resolves.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

from synthgen.config import load_config  # noqa: E402
from synthgen.data import build_dataset, time_split_windows  # noqa: E402
from synthgen.metrics import fidelity_report  # noqa: E402
from synthgen.pipeline import fit, generate  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="cloud")
    ap.add_argument("--epochs", type=int, default=None)
    args = ap.parse_args(argv)

    cfg_path = HERE / "configs" / "synthgen.yaml"
    cfg = load_config(mode=args.mode, path=cfg_path if cfg_path.exists() else None)
    if args.epochs is not None:
        cfg.diffusion.epochs = args.epochs

    # Force artifacts to AML's ``outputs/`` (captured automatically by the run).
    cfg.out_dir = "outputs"

    try:
        import mlflow

        mlflow.log_params(
            {
                "epochs": cfg.diffusion.epochs,
                "timesteps": cfg.diffusion.timesteps,
                "batch_size": cfg.diffusion.batch_size,
                "window": cfg.data.window,
                "device": cfg.device,
            }
        )
    except Exception:  # noqa: BLE001
        mlflow = None  # type: ignore[assignment]

    print(f"[train] mode={cfg.mode} device={cfg.device} epochs={cfg.diffusion.epochs}")
    ds = build_dataset(cfg)
    print(f"[train] windows={ds.n_windows} grid_rows={len(ds.grid)}")

    bundle = fit(cfg, ds=ds, save=True)

    # Evaluate fidelity on the held-out test windows vs synthetic.
    _w_tr, _r_tr, _w_va, _r_va, w_te, r_te = time_split_windows(ds, cfg)
    if len(w_te) > 0:
        synth = bundle.diffusion.sample(r_te, seed=cfg.diffusion.seed)
        synth_real = np.stack([bundle.scaler.inverse_transform(s) for s in synth])
        real_flat = w_te.reshape(-1, w_te.shape[2])
        synth_flat = synth_real.reshape(-1, synth_real.shape[2])
        rep = fidelity_report(
            real_flat,
            synth_flat,
            names=list(cfg.data.signals),
            real_windows=w_te,
            synth_windows=synth_real,
            n_states=cfg.diffusion.n_regimes,
        )
        summary = rep.summary()
        print(f"[train] fidelity={summary}")
        (Path(cfg.out_dir) / "fidelity.json").write_text(json.dumps(summary, indent=2))
        if mlflow is not None:
            mlflow.log_metrics({k: float(v) for k, v in summary.items() if v == v})

    print("[train] DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
