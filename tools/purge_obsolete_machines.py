"""Purge data for machines not in the active fleet from the KQL telemetry tables.

Drops all rows for M-003, M-004, M-005 from `raw_telemetry`, `anomalies`
and `injected_anomalies` so live correlation reflects only the active
M-001 / M-002 deployment.

Uses `.purge ... allowed` -> `.purge ... with (noregrets='true')` two-step.
Requires the caller to be Database Admin on the KQL DB.

Usage:
    python tools/purge_obsolete_machines.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"

KEEP_MACHINES = ("M-001", "M-002")
TABLES = ("raw_telemetry", "anomalies", "injected_anomalies")


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    load_dotenv(repo / ".env", override=True)

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
    print(f"[info] cluster: {query_uri}")
    print(f"[info] db     : {db_name}")
    print(f"[info] keep   : {KEEP_MACHINES}")

    keep_list = ", ".join(f"'{m}'" for m in KEEP_MACHINES)
    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(query_uri, cred)
    client = KustoClient(kcsb)

    for tbl in TABLES:
        predicate = f"where machine_id !in ({keep_list})"
        # Count first, just so the output is informative.
        try:
            r = client.execute(db_name, f"{tbl} | {predicate} | summarize n=count()")
            n = r.primary_results[0][0]["n"]
        except Exception as exc:
            print(f"[skip] {tbl}: count failed ({exc})")
            continue
        print(f"[info] {tbl}: {n:,} rows match deletion predicate")
        if n == 0:
            continue

        cmd = f".purge table {tbl} records <| {tbl} | {predicate}"
        print(f"[run]  .purge ... with (noregrets='true')")
        try:
            client.execute_mgmt(db_name, cmd + " with (noregrets='true')")
            print(f"[ok]   purge submitted on {tbl}")
        except Exception as exc:
            print(f"[err]  purge failed on {tbl}: {exc}")

    print("[done] purge requests submitted. Background purge can take minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
