"""Ad-hoc: dump (window_end, score, activity) for a machine over a lookback
using the lookback scorer, to characterise the activity signal and choose a
gate threshold. Buckets windows by activity to see score vs activity."""
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
MODEL = sys.argv[1] if len(sys.argv) > 1 else "transformer_ae_small__M-001"
MACH = sys.argv[2] if len(sys.argv) > 2 else "M-001"
LB = sys.argv[3] if len(sys.argv) > 3 else "2h"

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


print(f"=== {MACH} score vs activity buckets last {LB} ===")
show(f"""
score_multivariate_onnx_lookback('{MODEL}', '{MACH}', 1s, {LB}, 0.0)
| extend act_bucket = case(
    activity < 0.1, '0_off(<0.1)',
    activity < 0.3, '1_low(0.1-0.3)',
    activity < 0.7, '2_part(0.3-0.7)',
    activity < 1.3, '3_run(0.7-1.3)',
    '4_high(>1.3)')
| summarize windows=count(), score_p50=percentile(score,50),
    score_p95=percentile(score,95), score_max=max(score),
    act_min=min(activity), act_max=max(activity) by act_bucket
| order by act_bucket asc
""")
