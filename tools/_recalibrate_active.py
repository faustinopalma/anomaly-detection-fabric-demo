"""Recommend per-machine thresholds computed on ACTIVE windows only
(activity >= gate). For each machine, scores all windows over a lookback via
the lookback scorer, keeps active windows, and reports the normal-score
distribution plus a recommended threshold (margin above the active max)."""
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
GATE = 0.3
LB = sys.argv[1] if len(sys.argv) > 1 else "6h"
MODELS = {
    "M-001": "transformer_ae_small__M-001",
    "M-002": "transformer_ae_small__M-002",
    "M-003": "transformer_ae_small__M-003",
    "M-004": "transformer_ae_small__M-004",
}

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

print(f"Active-window (activity>={GATE}) score stats, lookback={LB}\n")
print(f"{'machine':8} {'n_act':>6} {'p50':>9} {'p95':>9} {'p99':>9} {'max':>9} "
      f"{'reco_thr':>9}")
for mach, model in MODELS.items():
    q = f"""
    score_multivariate_onnx_lookback('{model}', '{mach}', 1s, {LB}, 0.0)
    | where activity >= {GATE}
    | summarize n=count(), p50=percentile(score,50), p95=percentile(score,95),
        p99=percentile(score,99), mx=max(score)
    """
    try:
        t = c.execute(db, q).primary_results[0]
        r = list(t)[0]
        n = r["n"]
        if not n:
            print(f"{mach:8} {0:>6}   (no active windows)")
            continue
        mx = float(r["mx"]); p99 = float(r["p99"])
        # Recommended threshold: 1.5x the active max, floored at 1.3x p99.
        reco = max(mx * 1.5, p99 * 1.3)
        print(f"{mach:8} {n:>6} {float(r['p50']):>9.3f} {float(r['p95']):>9.3f} "
              f"{p99:>9.3f} {mx:>9.3f} {reco:>9.3f}")
    except Exception as e:
        print(f"{mach:8} ERROR: {e}")
