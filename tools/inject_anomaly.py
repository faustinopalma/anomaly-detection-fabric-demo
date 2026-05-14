"""Inject a synthetic spike directly into `raw_telemetry` to validate the
end-to-end scoring pipeline without touching the cloud simulator.

The script appends N contiguous samples for a single (machine, sensor) at a
constant `value`, starting one second after the latest existing `ts` for
that pair. The new extent triggers the update policy on `raw_telemetry`,
which calls `fn_score_demo()` and lands one or more rows into `anomalies`
when the score exceeds the model threshold.

Usage:
    python tools/inject_anomaly.py
    python tools/inject_anomaly.py --machine M-002 --sensor temperature_motor
    python tools/inject_anomaly.py --value 200 --samples 64 --quality 192

Defaults: machine=M-001, sensor=temperature_motor, value=150.0, samples=64,
quality=192. 64 samples is the model window size, so the injected slice
guarantees at least one all-spike window with a very high reconstruction
error.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

import requests
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def find_id(items: list[dict], name: str) -> str:
    for it in items:
        if it.get("displayName") == name:
            return it["id"]
    raise SystemExit(f"item '{name}' not found")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--machine", default="M-001")
    p.add_argument("--sensor", default="temperature_motor")
    p.add_argument("--value", type=float, default=150.0,
                   help="constant value for every injected sample")
    p.add_argument("--samples", type=int, default=64,
                   help="number of contiguous samples to inject (default 64 = window size)")
    p.add_argument("--quality", type=int, default=192,
                   help="OPC-UA quality code (default 192 = good)")
    args = p.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    db_name = os.environ["FABRIC_KQLDB_NAME"]

    cred = get_credential(tenant, FABRIC_SCOPE, repo_root)
    token = cred.get_token(FABRIC_SCOPE).token
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    ws = find_id(session.get(f"{API}/workspaces").json()["value"], ws_name)
    db_id = find_id(session.get(f"{API}/workspaces/{ws}/kqlDatabases").json()["value"], db_name)
    query_uri = session.get(f"{API}/workspaces/{ws}/kqlDatabases/{db_id}").json()[
        "properties"]["queryServiceUri"]

    client = KustoClient(KustoConnectionStringBuilder.with_azure_token_credential(query_uri, cred))

    # Find the latest ts already in the table for this (machine, sensor) so
    # the injected samples land in the future and form a contiguous slice.
    q = (f"raw_telemetry | where machine_id == '{args.machine}' "
         f"and sensor_id == '{args.sensor}' | summarize max_ts = max(ts)")
    res = client.execute(db_name, q).primary_results[0]
    if not res.rows or res.rows[0]["max_ts"] is None:
        raise SystemExit(f"no existing rows for {args.machine}/{args.sensor}")
    latest = res.rows[0]["max_ts"]
    start = latest + timedelta(seconds=1)
    print(f"[ok]   latest existing ts = {latest}")
    print(f"[run]  injecting {args.samples} samples at value={args.value}, "
          f"starting {start}")

    # Column order must match the table schema exactly:
    # machine_id, sensor_id, ts, value, quality, ingest_ts
    rows = []
    for i in range(args.samples):
        ts = (start + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rows.append(f"{args.machine},{args.sensor},{ts},{args.value},{args.quality},")
    csv_body = "\n".join(rows)

    client.execute_mgmt(db_name, f".ingest inline into table raw_telemetry <|\n{csv_body}")
    print(f"[ok]   inline ingest submitted ({args.samples} rows). "
          f"Anomalies typically appear in `anomalies` within ~60-90s.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
