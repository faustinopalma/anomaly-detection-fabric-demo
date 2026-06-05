"""Ad-hoc: inspect M-001 detections vs injections to characterise the FP pattern."""
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
WIN = sys.argv[2] if len(sys.argv) > 2 else "90m"

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


print(f"=== {MACH} detections last {WIN} (detected_at, score) ===")
show(f"anomalies | where machine_id=='{MACH}' and detected_at>ago({WIN}) "
     "| project detected_at, score | order by detected_at asc")
print(f"\n=== {MACH} injections last {WIN} ===")
show(f"injected_anomalies | where machine_id=='{MACH}' and start_ts>ago({WIN}) "
     "| project start_ts, expected_end_ts, anomaly_kind, sensor_target, duration_s "
     "| order by start_ts asc")
print(f"\n=== {MACH} anomalies table schema columns ===")
show("anomalies | getschema | project ColumnName, ColumnType")
