"""Ad-hoc: confirm M-002 FSM sensors stopped at cutover, mandrino_* current."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential  # noqa: E402

load_dotenv(override=True)
API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"
cred = get_credential(os.environ["FABRIC_TENANT_ID"], SCOPE, Path("."))
tok = cred.get_token(SCOPE).token
s = requests.Session()
s.headers.update({"Authorization": f"Bearer {tok}"})
ws = next(w for w in s.get(f"{API}/workspaces").json()["value"] if w["displayName"] == os.environ["FABRIC_WORKSPACE_NAME"])["id"]
db = next(d for d in s.get(f"{API}/workspaces/{ws}/kqlDatabases").json()["value"] if d["displayName"] == os.environ["FABRIC_KQLDB_NAME"])
cl = db["properties"]["queryServiceUri"]
k = KustoClient(KustoConnectionStringBuilder.with_azure_token_credential(cl, cred))
dbn = os.environ["FABRIC_KQLDB_NAME"]

print("=== M-002 last-seen timestamp per sensor ===")
q = (
    "raw_telemetry | where machine_id == 'M-002' "
    "| summarize last_seen=max(ts), n=count() by sensor_id | order by last_seen desc"
)
for r in k.execute(dbn, q).primary_results[0]:
    print(f"  {r['sensor_id']:<20} last={r['last_seen']}  n={r['n']}")
