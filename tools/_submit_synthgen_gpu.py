"""Submit the synthgen diffusion GPU training job to Azure ML.

Reproducible CLI equivalent of notebook 08 section 7. Stages a self-contained
snapshot (synthgen package + configs + real parquet) and submits the command
job to the T4 cluster, printing the job name immediately (non-blocking).

Usage:
    .venv/Scripts/python.exe tools/_submit_synthgen_gpu.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from synthgen import aml  # noqa: E402
from synthgen.config import load_config  # noqa: E402


def main() -> int:
    code_dir = REPO / "cloud-training-synth" / "src"
    conda_file = REPO / "cloud-training-synth" / "environment" / "conda.yml"

    # Local config knows the real parquet path under _data_local; used only to
    # stage the data into the job snapshot.
    local_cfg = load_config("local")
    aml.stage_cloud_bundle(local_cfg, code_dir)

    cloud_cfg = load_config("cloud")
    print(f"[submit] device={cloud_cfg.device} epochs={cloud_cfg.diffusion.epochs} "
          f"subset_days={cloud_cfg.subset_days} image={cloud_cfg.aml.environment_image}")
    job_name = aml.submit_training(
        cloud_cfg,
        code_dir=code_dir,
        conda_file=conda_file,
    )
    print("JOB NAME:", job_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
