"""One-shot: clear all data from telemetry tables (keeps `models`).

Uses `.clear table X data` (Table Admin), which is allowed for the principal
that created the tables. Unlike `.purge`, no Database Admin role required.

Destructive — but the live simulator (M-001, M-002) repopulates within
seconds. Historic injections in `injected_anomalies` are gone forever.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from azure.identity import AzureCliCredential
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

load_dotenv(override=True)
API = "https://api.fabric.microsoft.com/v1"
FAB_SCOPE = "https://api.fabric.microsoft.com/.default"

fab_cred = get_credential(os.environ["FABRIC_TENANT_ID"], FAB_SCOPE, Path("."))
fab_tok = fab_cred.get_token(FAB_SCOPE).token
sess = requests.Session()
sess.headers.update({"Authorization": f"Bearer {fab_tok}"})
ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
db_name = os.environ["FABRIC_KQLDB_NAME"]
ws = next(w for w in sess.get(f"{API}/workspaces").json()["value"] if w["displayName"] == ws_name)["id"]
dbs = sess.get(f"{API}/workspaces/{ws}/kqlDatabases").json().get("value", [])
db = next(d for d in dbs if d["displayName"] == db_name)
CLUSTER = db["properties"]["queryServiceUri"]
print(f"[ok] cluster={CLUSTER}  db={db_name}")

cred = AzureCliCredential()
kcsb = KustoConnectionStringBuilder.with_token_provider(
    CLUSTER, lambda: cred.get_token(CLUSTER + "/.default").token
)
client = KustoClient(kcsb)

TABLES = ["raw_telemetry", "anomalies", "injected_anomalies"]
for tbl in TABLES:
    cmd = f".clear table {tbl} data"
    print(f"[run] {cmd}")
    try:
        client.execute_mgmt(db_name, cmd)
        print(f"[ok]  cleared {tbl}")
    except Exception as e:  # noqa: BLE001
        print(f"[fail] {tbl}: {e}")

# Quick sanity counts
for tbl in TABLES:
    try:
        r = list(client.execute(db_name, f"{tbl} | count").primary_results[0])
        print(f"  {tbl}: {r[0]['Count']} rows remaining")
    except Exception as e:  # noqa: BLE001
        print(f"  {tbl}: count failed: {e}")
