"""Azure ML entry script: run the SOTA CNC anomaly sweep (``tools/cnc_ae_lab``)
for M-003 and M-002 on the compute node, then publish the exported model
artifacts to the job's output folder so they can be downloaded back into the
repo's ``models/``.

The job's *code* folder is a minimal mirror of the repo so that
``cnc_ae_lab.REPO`` resolves correctly:

    <code>/entry_cnc_sota.py
    <code>/tools/cnc_ae_lab.py
    <code>/tools/train_cnc_m003.py
    <code>/simulator-local/cnc_engine.py
    <code>/data/cnc_profile_M-003.json
    <code>/simulator-cloud/src/synth_trace_M-002.json

``cnc_ae_lab.py`` writes its artifacts to ``<REPO>/models/<name>/`` (here
``<code>/models``); after each run we copy them into ``$AZUREML_OUTPUT_DIR`` so
the driver can download them. This keeps a single source of truth: the exact
same lab code runs locally and in the cloud.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--machines", nargs="+", default=["M-003", "M-002"])
    ap.add_argument("--epochs", type=int, default=18)
    ap.add_argument("--hours", type=float, default=120.0)
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    out_root = Path(os.environ.get("AZUREML_OUTPUT_DIR", "outputs"))
    out_root.mkdir(parents=True, exist_ok=True)

    try:
        import torch  # noqa: PLC0415
        print(f"[entry] torch={torch.__version__} cuda={torch.cuda.is_available()}",
              flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[entry] torch import failed: {exc}", flush=True)

    lab = HERE / "tools" / "cnc_ae_lab.py"
    rc_total = 0
    for machine in args.machines:
        cmd = [sys.executable, "-u", str(lab), machine,
               "--epochs", str(args.epochs), "--hours", str(args.hours), "--save"]
        if args.quick:
            cmd.append("--quick")
        print(f"\n[entry] ===== {machine} =====", flush=True)
        print(f"[entry] >>> {' '.join(cmd)}", flush=True)
        rc = subprocess.call(cmd, cwd=str(HERE))
        print(f"[entry] {machine} finished rc={rc}", flush=True)
        if rc != 0:
            rc_total = rc

    models_dir = HERE / "models"
    published = []
    if models_dir.exists():
        for d in sorted(models_dir.iterdir()):
            if d.is_dir() and d.name.startswith("transformer_ae_small__"):
                dst = out_root / d.name
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(d, dst)
                published.append(d.name)
                print(f"[entry] published {d.name} -> output", flush=True)
    print(f"[entry] published artifacts: {published}", flush=True)
    if not published:
        print("[entry] WARNING: no artifacts were produced", flush=True)
        return rc_total or 4
    return rc_total


if __name__ == "__main__":
    raise SystemExit(main())
