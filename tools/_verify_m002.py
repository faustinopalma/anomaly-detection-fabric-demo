"""Ad-hoc: verify M-002 synthgen ingest + scoring after redeploy."""
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
ws_name = os.environ["FABRIC_WORKSPACE_NAME"]
db_name = os.environ["FABRIC_KQLDB_NAME"]
ws = next(w for w in s.get(f"{API}/workspaces").json()["value"] if w["displayName"] == ws_name)["id"]
db = next(d for d in s.get(f"{API}/workspaces/{ws}/kqlDatabases").json()["value"] if d["displayName"] == db_name)
cluster = db["properties"]["queryServiceUri"]

kcsb = KustoConnectionStringBuilder.with_azure_token_credential(cluster, cred)
client = KustoClient(kcsb)

print("=== M-002 raw_telemetry sensors (last 5 min) ===")
q = (
    "raw_telemetry | where machine_id == 'M-002' and ts > ago(5m) "
    "| summarize n=count(), min_v=round(min(value),1), max_v=round(max(value),1) by sensor_id "
    "| order by sensor_id asc"
)
for r in client.execute(db_name, q).primary_results[0]:
    print(f"  {r['sensor_id']:<18} n={r['n']:<6} range=[{r['min_v']}, {r['max_v']}]")

print("\n=== per-machine row counts (last 5 min) ===")
q = "raw_telemetry | where ts > ago(5m) | summarize n=count(), sensors=dcount(sensor_id) by machine_id | order by machine_id asc"
for r in client.execute(db_name, q).primary_results[0]:
    print(f"  {r['machine_id']}  rows={r['n']}  sensors={r['sensors']}")

print("\n=== anomalies by machine (last 10 min) ===")
q = "anomalies | where detected_at > ago(10m) | summarize n=count(), maxscore=round(max(score),3) by machine_id, model_name | order by machine_id asc"
rows = list(client.execute(db_name, q).primary_results[0])
if not rows:
    print("  (none yet)")
for r in rows:
    print(f"  {r['machine_id']}  model={r['model_name']}  n={r['n']}  maxscore={r['maxscore']}")
