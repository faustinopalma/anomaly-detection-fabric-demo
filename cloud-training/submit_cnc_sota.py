"""Driver: submit the SOTA CNC anomaly sweep (``tools/cnc_ae_lab.py``) to Azure
ML, stream the run to completion, and download the exported model artifacts
into the repo's ``models/`` folder.

This reuses :mod:`submit_job` for the workspace client, environment and the
AAD-based artifact download, so there is a single source of truth for the AML
plumbing. The only thing that differs is the *code* that runs on the node: a
minimal mirror of the repo containing the lab and its data dependencies (see
``src_cnc_sota/entry_cnc_sota.py``).

Usage:
    .venv/Scripts/python.exe cloud-training/submit_cnc_sota.py
    .venv/Scripts/python.exe cloud-training/submit_cnc_sota.py --quick
    .venv/Scripts/python.exe cloud-training/submit_cnc_sota.py --machines M-003
"""

from __future__ import annotations

import argparse
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

from azure.ai.ml import command
from azure.ai.ml.entities import Environment

import submit_job as sj  # get_client / download_models / workspace config

REPO = Path(__file__).resolve().parents[1]
# Stage outside OneDrive to avoid sync handles locking rmtree on Windows.
STAGING = Path(tempfile.gettempdir()) / "anomaly_cnc_sota_staging"

# (repo-relative source, staging-relative destination) — the minimal mirror the
# lab needs so cnc_ae_lab.REPO (== staging root) resolves every dependency.
STAGE_FILES = [
    ("cloud-training/src_cnc_sota/entry_cnc_sota.py", "entry_cnc_sota.py"),
    ("tools/cnc_ae_lab.py", "tools/cnc_ae_lab.py"),
    ("tools/train_cnc_m003.py", "tools/train_cnc_m003.py"),
    ("simulator-local/cnc_engine.py", "simulator-local/cnc_engine.py"),
    ("data/cnc_profile_M-003.json", "data/cnc_profile_M-003.json"),
    ("simulator-cloud/src/synth_trace_M-002.json",
     "simulator-cloud/src/synth_trace_M-002.json"),
    # Full synthgen training trace for M-002 (86k samples; the 4h sim slice is
    # too short). Same generator bundle the simulator replays -> train/serve safe.
    ("_local/synthgen/synth_trace_full.npz",
     "_local/synthgen/synth_trace_full.npz"),
]


def _force_rmtree(path: Path) -> None:
    """Remove a tree even when files carry the read-only attribute (Windows)."""
    def _on_error(func, p, _exc):  # noqa: ANN001
        try:
            import os  # noqa: PLC0415
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(path, onerror=_on_error)


def stage_code() -> Path:
    if STAGING.exists():
        _force_rmtree(STAGING)
    STAGING.mkdir(parents=True)
    for src_rel, dst_rel in STAGE_FILES:
        src = REPO / src_rel
        dst = STAGING / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print(f"[stage] mirrored {len(STAGE_FILES)} files into {STAGING}")
    return STAGING


def build_job(machines: list[str], epochs: int, hours: float, quick: bool,
              compute: str):
    env = Environment(
        name="anomaly-train-env",
        image="mcr.microsoft.com/azureml/openmpi4.1.0-ubuntu20.04:latest",
        conda_file=str(REPO / "cloud-training" / "environment" / "conda.yml"),
    )
    cmd = (
        "python entry_cnc_sota.py "
        f"--machines {' '.join(machines)} --epochs {epochs} --hours {hours}"
    )
    if quick:
        cmd += " --quick"
    return command(
        code=str(STAGING),
        command=cmd,
        environment=env,
        compute=compute,
        display_name="anomaly-cnc-sota-sweep",
        experiment_name="anomaly-detection",
    )


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--machines", nargs="+", default=["M-003", "M-002"])
    ap.add_argument("--epochs", type=int, default=18)
    ap.add_argument("--hours", type=float, default=120.0)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--compute", default=sj.COMPUTE,
                    help="AML compute target (e.g. gpu-t4-cluster).")
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args(argv)

    ml = sj.get_client()
    print(f"[submit] workspace={sj.WORKSPACE} rg={sj.RESOURCE_GROUP} "
          f"compute={args.compute}")
    print(f"[submit] machines={args.machines} epochs={args.epochs} "
          f"hours={args.hours} quick={args.quick}")

    stage_code()
    job = ml.jobs.create_or_update(
        build_job(args.machines, args.epochs, args.hours, args.quick,
                  args.compute))
    print(f"[submit] job name: {job.name}")
    print(f"[submit] studio : {job.studio_url}")

    if args.no_wait:
        return 0

    # Stream the live logs (best-effort) so failures are diagnosable inline.
    try:
        ml.jobs.stream(job.name)
    except Exception as exc:  # noqa: BLE001
        print(f"[submit] stream ended ({exc}); falling back to polling")

    terminal = {"Completed", "Failed", "Canceled"}
    last = None
    while True:
        j = ml.jobs.get(job.name)
        if j.status != last:
            print(f"[status] {j.status}")
            last = j.status
        if j.status in terminal:
            break
        time.sleep(15)

    if last == "Completed":
        print("[submit] job completed; downloading artifacts...")
        sj.download_models(ml, job.name)
        print("[submit] DONE")
        return 0

    print(f"[submit] job ended with status={last}")
    print(f"[submit] inspect logs in studio: {job.studio_url}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
