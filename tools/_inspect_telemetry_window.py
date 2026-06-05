"""Ad-hoc: per-sensor telemetry stats for a machine over two time windows, to
check whether a high-score 'false positive' burst corresponds to out-of-range
sensors (real anomaly) or in-range values (model generalisation gap)."""
from __future__ import annotations
import os, sys
from pathlib import Path
import requests
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from dotenv import load_dotenv
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fabric_auth import get_credential

API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"
MACH = sys.argv[1] if len(sys.argv) > 1 else "M-001"

root = Path(__file__).resolve().parent.parent
load_dotenv(root / ".env")
cred = get_credential(os.environ["FABRIC_TENANT_ID"], SCOPE, root)
tok = cred.get_token(SCOPE).token
s = requests.Session(); s.headers.update({"Authorization": f"Bearer {tok}"})
wsid = next(w["id"] for w in s.get(f"{API}/workspaces").json()["value"]
            if w["displayName"] == os.environ["FABRIC_WORKSPACE_NAME"])
dbid = next(d["id"] for d in s.get(f"{API}/workspaces/{wsid}/kqlDatabases").json()["value"]
            if d["displayName"] == os.environ["FABRIC_KQLDB_NAME"])
uri = s.get(f"{API}/workspaces/{wsid}/kqlDatabases/{dbid}").json()["properties"]["queryServiceUri"]
c = KustoClient(KustoConnectionStringBuilder.with_azure_token_credential(uri, cred))
db = os.environ["FABRIC_KQLDB_NAME"]


def show(q: str) -> None:
    t = c.execute(db, q).primary_results[0]
    cols = [x.column_name for x in t.columns]
    print("   " + " | ".join(cols))
    for r in t:
        print("   " + " | ".join(str(r[x]) for x in cols))


# Burst window (high score) vs calm window (normal score), from the inspection.
print(f"=== {MACH} per-sensor stats during HIGH-score burst 07:20-07:31 ===")
show(f"raw_telemetry | where machine_id=='{MACH}' "
     "and ts between (datetime(2026-06-05 07:20:00) .. datetime(2026-06-05 07:31:00)) "
     "and sensor_id !startswith '__inject__' "
     "| summarize cnt=count(), mn=min(value), avg=avg(value), mx=max(value) by sensor_id "
     "| order by sensor_id asc")
print(f"\n=== {MACH} per-sensor stats during CALM window 07:40-07:50 (score ~1.1) ===")
show(f"raw_telemetry | where machine_id=='{MACH}' "
     "and ts between (datetime(2026-06-05 07:40:00) .. datetime(2026-06-05 07:50:00)) "
     "and sensor_id !startswith '__inject__' "
     "| summarize cnt=count(), mn=min(value), avg=avg(value), mx=max(value) by sensor_id "
     "| order by sensor_id asc")
