"""Quick verifier: poll raw_telemetry until rows appear (or timeout)."""
from __future__ import annotations

import os
import sys
import time
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

# Discover cluster URL from Fabric
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
DB = db_name
print(f"[ok] cluster={CLUSTER}  db={DB}")

cred = AzureCliCredential()
kcsb = KustoConnectionStringBuilder.with_token_provider(
    CLUSTER, lambda: cred.get_token(CLUSTER + "/.default").token
)
client = KustoClient(kcsb)

QUERY = (
    "raw_telemetry | summarize n=count(), latest=max(ts), "
    "max_ingest=max(ingest_ts) by machine_id "
    "| order by machine_id asc"
)

for attempt in range(1, 9):
    res = client.execute(DB, QUERY)
    rows = list(res.primary_results[0])
    print(f"attempt {attempt}: {len(rows)} machines")
    for row in rows:
        print(f"  {row['machine_id']}: n={row['n']} latest_ts={row['latest']} max_ingest={row['max_ingest']}")
    if rows:
        break
    time.sleep(15)
