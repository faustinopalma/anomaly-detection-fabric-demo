"""Ad-hoc: dump M-004 would-be fires (window_end, score, activity) and the
injection windows, to classify the 2 residual FPs (timing near-miss vs genuine
high-score active false alarm)."""
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
MACH = sys.argv[1] if len(sys.argv) > 1 else "M-004"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "transformer_ae_small__M-004"
THR = float(sys.argv[3]) if len(sys.argv) > 3 else 12.0
LB = sys.argv[4] if len(sys.argv) > 4 else "3h"

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


print(f"=== {MACH} would-be fires (activity>=0.3, score>{THR}) last {LB} ===")
show(f"score_multivariate_onnx_lookback('{MODEL}', '{MACH}', 1s, {LB}, {THR}) "
     f"| where activity >= 0.3 and score > {THR} "
     "| project window_start, window_end, score, activity | order by window_end asc")
print(f"\n=== {MACH} injections last {LB} (match band = start-30s .. end+120s) ===")
show(f"injected_anomalies | where machine_id=='{MACH}' and start_ts>ago({LB}) "
     "| project band_lo=start_ts-30s, start_ts, expected_end_ts, band_hi=expected_end_ts+120s, "
     "anomaly_kind, sensor_target | order by start_ts asc")
