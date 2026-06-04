"""Azure ML helpers — submit the diffusion training, poll, and download.

Thin wrappers around ``azure-ai-ml`` so the orchestrator notebook stays free of
SDK boilerplate. Authentication uses the logged-in Azure CLI identity
(``AzureCliCredential``) with a ``DefaultAzureCredential`` fallback. Artifact
download reads the run's output blobs directly with AAD because the AML default
storage account has ``allowSharedKeyAccess=false`` (mirrors the existing
``cloud-training/submit_job.py`` pattern).

Design: ``submit_training`` returns *immediately* with the job name; ``poll`` and
``download_model`` are idempotent and can be re-run from independent notebook
cells.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .config import REPO_ROOT, Config

TERMINAL_STATES = {"Completed", "Failed", "Canceled"}


def _force_rmtree(path: Path) -> None:
    """Remove ``path`` robustly, clearing read-only bits (Windows/OneDrive)."""

    def _on_error(func: Any, target: str, _exc: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    # ``onexc`` (3.12+) supersedes ``onerror``; pass via kwargs for portability.
    try:
        shutil.rmtree(path, onexc=_on_error)  # type: ignore[call-arg]
    except TypeError:
        shutil.rmtree(path, onerror=lambda f, p, e: _on_error(f, p, e))


def stage_cloud_bundle(cfg: Config, code_dir: str | Path) -> Path:
    """Assemble a self-contained job snapshot under ``code_dir``.

    Copies the ``synthgen`` package, the ``configs/`` folder and the real wide
    parquet into the upload directory so the AML job is fully reproducible from
    its own snapshot (the data lives only locally in ``_data_local``). Returns
    the resolved snapshot path.
    """
    out = Path(code_dir)
    out = out if out.is_absolute() else (REPO_ROOT / out)
    out.mkdir(parents=True, exist_ok=True)

    # synthgen package (skip caches).
    pkg_src = REPO_ROOT / "synthgen"
    pkg_dst = out / "synthgen"
    if pkg_dst.exists():
        _force_rmtree(pkg_dst)
    shutil.copytree(pkg_src, pkg_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # configs.
    cfg_dst = out / "configs"
    cfg_dst.mkdir(exist_ok=True)
    shutil.copy2(REPO_ROOT / "configs" / "synthgen.yaml", cfg_dst / "synthgen.yaml")

    # data (the cloud config points at data/cnc_real_wide.parquet).
    data_dst = out / "data"
    data_dst.mkdir(exist_ok=True)
    shutil.copy2(cfg.resolve(cfg.data.wide_path), data_dst / "cnc_real_wide.parquet")

    print(f"[aml] staged cloud bundle -> {out}")
    return out



def get_client(cfg: Config):
    """Return an authenticated ``MLClient`` for the configured workspace."""
    from azure.ai.ml import MLClient
    from azure.identity import AzureCliCredential, DefaultAzureCredential

    try:
        cred: Any = AzureCliCredential()
        cred.get_token("https://management.azure.com/.default")
    except Exception:  # noqa: BLE001
        cred = DefaultAzureCredential()
    return MLClient(cred, cfg.aml.subscription, cfg.aml.resource_group, cfg.aml.workspace)


def submit_training(
    cfg: Config,
    *,
    code_dir: str | Path,
    conda_file: str | Path,
    epochs: int | None = None,
    display_name: str = "synthgen-diffusion-train",
) -> str:
    """Submit the diffusion training command job; return its name immediately.

    The job runs ``train_diffusion.py --mode cloud`` on the GPU cluster. It does
    NOT wait for completion — poll separately with :func:`poll`.
    """
    from azure.ai.ml import MLClient, command
    from azure.ai.ml.entities import Environment

    ml: MLClient = get_client(cfg)
    env = Environment(
        name="synthgen-train-env",
        image=cfg.aml.environment_image,
        conda_file=str(conda_file),
    )
    epochs_arg = f" --epochs {epochs}" if epochs is not None else ""
    job = command(
        code=str(code_dir),
        command=f"python train_diffusion.py --mode cloud{epochs_arg}",
        environment=env,
        compute=cfg.aml.gpu_cluster,
        display_name=display_name,
        experiment_name=cfg.aml.experiment_name,
    )
    created = ml.jobs.create_or_update(job)
    print(f"[aml] submitted job: {created.name}")
    print(f"[aml] studio: {created.studio_url}")
    return created.name


def poll(cfg: Config, job_name: str) -> str:
    """Return the current status of a job (single, non-blocking check)."""
    ml = get_client(cfg)
    status = ml.jobs.get(job_name).status
    print(f"[aml] {job_name} -> {status}")
    return status


def stream(cfg: Config, job_name: str) -> None:
    """Stream a job's logs to stdout until it reaches a terminal state."""
    ml = get_client(cfg)
    ml.jobs.stream(job_name)


def download_model(cfg: Config, job_name: str, dest: str | Path | None = None) -> Path:
    """Download the run's ``outputs/`` artifacts via AAD blob auth.

    Returns the local directory containing the downloaded model (scaler.json,
    regime.json, timing.json, diffusion.pt, metadata).
    """
    from azure.identity import AzureCliCredential
    from azure.storage.blob import BlobServiceClient

    ml = get_client(cfg)
    store = ml.datastores.get_default()
    cred = AzureCliCredential()
    bsc = BlobServiceClient(
        f"https://{store.account_name}.blob.core.windows.net", credential=cred
    )
    cont = bsc.get_container_client(store.container_name)

    prefix = f"azureml/{job_name}/"
    blobs = [b.name for b in cont.list_blobs(name_starts_with=prefix)]
    if not blobs:
        prefix = f"ExperimentRun/dcid.{job_name}/outputs/"
        blobs = [b.name for b in cont.list_blobs(name_starts_with=prefix)]
    if not blobs:
        raise FileNotFoundError(f"No output blobs found for job {job_name}")

    out = Path(dest) if dest is not None else (cfg.out_path / "cloud_model")
    out.mkdir(parents=True, exist_ok=True)
    for name in blobs:
        rel = name[len(prefix):].lstrip("/")
        # Keep only the model artifacts (flatten any leading 'outputs/').
        rel = rel[len("outputs/"):] if rel.startswith("outputs/") else rel
        if not rel:
            continue
        local = out / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(cont.download_blob(name).readall())
    print(f"[aml] downloaded {len(blobs)} blobs -> {out}")
    return out
