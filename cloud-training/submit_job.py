"""Submit the per-machine training command job to Azure ML using the Python
SDK (azure-ai-ml), stream it to completion, and download the produced model
artifacts into the repo's ``models/`` folder.

This avoids the ``az ml`` CLI extension entirely. Auth uses the already
logged-in Azure CLI identity (AzureCliCredential), falling back to
DefaultAzureCredential.

Usage:
    .venv/Scripts/python.exe cloud-training/submit_job.py
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from azure.ai.ml import MLClient, command
from azure.ai.ml.entities import Environment
from azure.identity import AzureCliCredential, DefaultAzureCredential

REPO = Path(__file__).resolve().parents[1]
SUBSCRIPTION = "7ecf802f-04ac-4e81-8703-c3d39074f823"
RESOURCE_GROUP = "rg-anomaly-ml-westeurope"
WORKSPACE = "anomalyml-mlw"
COMPUTE = "cpu-cluster"
MACHINES = ["M-001", "M-002"]


def get_client() -> MLClient:
    try:
        cred = AzureCliCredential()
        cred.get_token("https://management.azure.com/.default")
    except Exception:  # noqa: BLE001
        cred = DefaultAzureCredential()
    return MLClient(cred, SUBSCRIPTION, RESOURCE_GROUP, WORKSPACE)


def build_job(epochs: int, hours: float):
    env = Environment(
        name="anomaly-train-env",
        image="mcr.microsoft.com/azureml/openmpi4.1.0-ubuntu20.04:latest",
        conda_file=str(REPO / "cloud-training" / "environment" / "conda.yml"),
    )
    machines = " ".join(MACHINES)
    return command(
        code=str(REPO / "cloud-training" / "src"),
        command=(
            "python generate_and_train.py "
            f"--machines {machines} --hours {hours} --epochs {epochs}"
        ),
        environment=env,
        compute=COMPUTE,
        display_name="anomaly-per-machine-ae-training",
        experiment_name="anomaly-detection",
    )


def download_models(ml: MLClient, job_name: str) -> None:
    """Download produced artifacts via Entra (AAD) blob auth.

    The AML default storage account has ``allowSharedKeyAccess=false``, so the
    SDK's key-based ``jobs.download`` fails. We read the run's output blobs
    directly with AzureCliCredential (requires Storage Blob Data Reader).
    """
    from azure.storage.blob import BlobServiceClient

    store = ml.datastores.get_default()
    account = store.account_name
    container = store.container_name
    cred = AzureCliCredential()
    bsc = BlobServiceClient(
        f"https://{account}.blob.core.windows.net", credential=cred)
    cont = bsc.get_container_client(container)

    prefix = f"azureml/{job_name}/"  # default output uri_folder location
    blobs = [b.name for b in cont.list_blobs(name_starts_with=prefix)]
    if not blobs:
        # fall back to ExperimentRun artifact path
        prefix = f"ExperimentRun/dcid.{job_name}/outputs/"
        blobs = [b.name for b in cont.list_blobs(name_starts_with=prefix)]

    copied: set[str] = set()
    for name in blobs:
        rel = name[len(prefix):]
        if "transformer_ae_small__" not in rel and "training_summary" not in rel:
            continue
        local = REPO / "_local" / "job_outputs" / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        with open(local, "wb") as fh:
            fh.write(cont.download_blob(name).readall())
        copied.add(rel.split("/")[0])

    for art_name in sorted(copied):
        src = REPO / "_local" / "job_outputs" / art_name
        if not src.is_dir():
            continue
        target = REPO / "models" / art_name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target)
        print(f"[download] {art_name} -> models/{art_name}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args(argv)

    ml = get_client()
    print(f"[submit] workspace={WORKSPACE} rg={RESOURCE_GROUP} compute={COMPUTE}")

    job = ml.jobs.create_or_update(build_job(args.epochs, args.hours))
    print(f"[submit] job name: {job.name}")
    print(f"[submit] studio : {job.studio_url}")

    if args.no_wait:
        return 0

    terminal = {"Completed", "Failed", "Canceled"}
    last = None
    while True:
        j = ml.jobs.get(job.name)
        if j.status != last:
            print(f"[status] {j.status}")
            last = j.status
        if j.status in terminal:
            break
        time.sleep(20)

    if last == "Completed":
        print("[submit] job completed; downloading artifacts...")
        download_models(ml, job.name)
        print("[submit] DONE")
        return 0
    print(f"[submit] job ended with status={last}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
