"""Register a locally trained ONNX model into the Kusto `models` table.

Reads `<model_dir>/model.fp16.onnx` (or `model.onnx`) + `metadata.json`
+ `scaler.json`, base64-encodes the ONNX bytes, then ingests one row
into the `models` table. The new `version` is `max(existing)+1`, so
re-runs cleanly bump the version and `latest_model()` always points to
the most recent registration.

Usage:
    python tools/05_register_model.py models/transformer_ae_small
    python tools/05_register_model.py models/transformer_ae_small --name transformer_ae_small --fp32
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import requests
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir", type=Path)
    ap.add_argument("--name", default=None,
                    help="Override the model name (default: metadata['model'] or dir name)")
    ap.add_argument("--fp32", action="store_true",
                    help="Use model.onnx instead of model.fp16.onnx")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parent.parent
    load_dotenv(repo / ".env", override=True)

    mdir = args.model_dir if args.model_dir.is_absolute() else (repo / args.model_dir)
    if not mdir.is_dir():
        raise SystemExit(f"Model dir not found: {mdir}")

    onnx_path = mdir / ("model.onnx" if args.fp32 else "model.fp16.onnx")
    meta_path = mdir / "metadata.json"
    scaler_path = mdir / "scaler.json"
    for p in (onnx_path, meta_path, scaler_path):
        if not p.is_file():
            raise SystemExit(f"Missing artifact: {p}")

    meta = json.loads(meta_path.read_text())
    scaler = json.loads(scaler_path.read_text())
    name = args.name or meta.get("model") or mdir.name
    window_size = int(meta["window"])
    sensors = scaler["sensors"]
    payload_b64 = base64.b64encode(onnx_path.read_bytes()).decode("ascii")

    # Embed scaler so KQL-side python can de-normalize if it ever wants.
    full_meta = {**meta, "scaler": scaler}

    print(f"[info] model dir : {mdir.relative_to(repo)}")
    print(f"[info] onnx file : {onnx_path.name}  ({onnx_path.stat().st_size/1024:.1f} KB raw, "
          f"{len(payload_b64)/1024:.1f} KB base64)")
    print(f"[info] name      : {name}")
    print(f"[info] window    : {window_size}  sensors={sensors}")

    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    db_name = os.environ["FABRIC_KQLDB_NAME"]
    cred = get_credential(tenant, SCOPE, repo)
    tok = cred.get_token(SCOPE).token

    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {tok}"})
    ws = next(w for w in s.get(f"{API}/workspaces").json()["value"]
              if w["displayName"] == ws_name)["id"]
    kdb = next(d for d in s.get(f"{API}/workspaces/{ws}/kqlDatabases").json()["value"]
               if d["displayName"] == db_name)
    query_uri = kdb["properties"]["queryServiceUri"]
    ingest_uri = query_uri.replace("https://trd-", "https://ingest-trd-", 1)
    print(f"[info] cluster   : {query_uri}")

    kcsb_q = KustoConnectionStringBuilder.with_azure_token_credential(query_uri, cred)
    client = KustoClient(kcsb_q)

    # Find current max version for this model name
    r = client.execute(db_name, f"models | where name == '{name}' | summarize v=max(version)")
    cur = r.primary_results[0][0]["v"] if len(r.primary_results[0]) else None
    new_ver = (int(cur) if cur is not None else 0) + 1
    print(f"[info] new version: {new_ver}  (previous: {cur})")

    # Use inline ingestion via management command — simplest, no blob upload.
    # Build a single-row TSV-like CSV. ONNX base64 is ASCII so no escaping risk.
    sensors_json = json.dumps(sensors).replace('"', '""')
    metadata_json = json.dumps(full_meta, default=str).replace('"', '""')
    row = (
        f'"{name}",{new_ver},datetime(now),"onnx",{window_size},'
        f'"{sensors_json}","{payload_b64}","{metadata_json}"'
    )
    cmd = (
        ".ingest inline into table models with (format='csv') <|\n"
        + row
    )
    print(f"[run]  ingesting (cmd size: {len(cmd)/1024:.1f} KB)...")
    client.execute_mgmt(db_name, cmd)
    print("[ok]   ingested. Verifying...")

    chk = client.execute(
        db_name,
        f"models | where name == '{name}' | summarize v=max(version), b=max(strlen(payload))"
    )
    row = chk.primary_results[0][0]
    print(f"[ok]   models[name={name!r}] max version={row['v']}, payload b64 len={row['b']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
