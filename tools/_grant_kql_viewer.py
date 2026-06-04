"""One-shot: grant a managed-identity (aadapp principalId) viewer access to the
Fabric KQL database so the simulator's Fabric poller can read `anomalies`.

Usage:
    python tools/_grant_kql_viewer.py <principalId> [<database>]

The cluster URI is resolved from the Fabric API (same as the other tools).
This script is safe to re-run; granting an existing viewer is idempotent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

API = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def main(argv: list[str]) -> int:
    if not argv:
        print("Usage: _grant_kql_viewer.py <principalId> [<database>]", file=sys.stderr)
        return 2

    principal_id = argv[0]
    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")

    tenant = os.environ["FABRIC_TENANT_ID"]
    ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
    db_name = argv[1] if len(argv) > 1 else os.environ["FABRIC_KQLDB_NAME"]

    cred = get_credential(tenant, FABRIC_SCOPE, repo_root)
    token = cred.get_token(FABRIC_SCOPE).token
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    ws = next(
        w["id"]
        for w in session.get(f"{API}/workspaces").json()["value"]
        if w["displayName"] == ws_name
    )
    db_id = next(
        d["id"]
        for d in session.get(f"{API}/workspaces/{ws}/kqlDatabases").json()["value"]
        if d["displayName"] == db_name
    )
    meta = session.get(f"{API}/workspaces/{ws}/kqlDatabases/{db_id}").json()
    query_uri = meta["properties"]["queryServiceUri"]
    print(f"[info] cluster: {query_uri}")

    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(query_uri, cred)
    client = KustoClient(kcsb)

    cmd = (
        f".add database ['{db_name}'] viewers "
        f"('aadapp={principal_id}') 'ca-simulator fabric poller'"
    )
    print(f"[run]  {cmd}")
    result = client.execute_mgmt(db_name, cmd)
    table = result.primary_results[0]
    cols = [c.column_name for c in table.columns]
    for row in table:
        print(f"[ok]   {dict(zip(cols, row))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
