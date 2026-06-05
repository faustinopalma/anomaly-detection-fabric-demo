"""Separate M-004 ACTIVE windows into normal vs in-injection-band, to choose a
threshold that catches real injections while staying above normal-active noise.
Uses the lookback scorer (returns score + activity), joins to injection bands."""
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
LB = sys.argv[3] if len(sys.argv) > 3 else "12h"
GATE = 0.5

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


# Normal-active vs in-band-active score distributions
print(f"=== {MACH} active(>= {GATE}) score: NORMAL vs IN-INJECTION-BAND, last {LB} ===")
show(f"""
let inj = injected_anomalies
    | where machine_id == '{MACH}' and start_ts > ago({LB})
    | project lo = start_ts - 30s, hi = expected_end_ts + 120s, sensor_target;
let w = score_multivariate_onnx_lookback('{MODEL}', '{MACH}', 1s, {LB}, 0.0)
    | where activity >= {GATE}
    | project window_end, score;
w
| extend dummy = 1
| join kind=leftouter (inj | extend dummy = 1) on dummy
| summarize in_band = max(iff(window_end between (lo .. hi), 1, 0)) by window_end, score
| summarize n = count(), p50 = percentile(score,50), p90 = percentile(score,90),
    p99 = percentile(score,99), mx = max(score) by in_band
| order by in_band asc
""")

# Per-injection-band: did any active window cross various thresholds?
print(f"\n=== {MACH} per-injection: max active score in band, last {LB} ===")
show(f"""
let w = score_multivariate_onnx_lookback('{MODEL}', '{MACH}', 1s, {LB}, 0.0)
    | where activity >= {GATE}
    | project window_end, score;
injected_anomalies
| where machine_id == '{MACH}' and start_ts > ago({LB})
| project start_ts, sensor_target, anomaly_kind, lo = start_ts - 30s, hi = expected_end_ts + 120s
| extend dummy = 1
| join kind=leftouter (w | extend dummy = 1) on dummy
| where window_end between (lo .. hi)
| summarize max_score = max(score), n_win = count() by start_ts, sensor_target, anomaly_kind
| order by start_ts asc
""")
