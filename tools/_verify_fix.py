"""Verify the post-fix detection logic (activity gate + per-model threshold)
against the live data, WITHOUT waiting for new ingestion. For each machine it
reproduces what the update policy would now emit: windows with activity>=GATE
and score>threshold(from models table), then matches them to injection windows
[start-30s, expected_end+120s]. Reports would-be true/false positives."""
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
LB = sys.argv[1] if len(sys.argv) > 1 else "3h"
GATE = float(sys.argv[2]) if len(sys.argv) > 2 else 0.3
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


def scalar(q: str):
    return list(c.execute(db, q).primary_results[0])[0][0]


print(f"Post-fix would-be detections (gate activity>={GATE}, live threshold), "
      f"lookback={LB}\n")
print(f"{'machine':8} {'thr':>7} {'fires':>6} {'TP':>4} {'FP':>4} {'injExpd':>8}")
for mach, model in MODELS.items():
    thr = float(scalar(
        f"latest_model('{model}') | project todouble(metadata.threshold)"))
    q = f"""
    let gate = {GATE};
    let fires = score_multivariate_onnx_lookback('{model}', '{mach}', 1s, {LB}, {thr})
        | where activity >= gate and score > {thr}
        | project window_start, window_end;
    let inj = injected_anomalies
        | where machine_id == '{mach}' and start_ts > ago({LB})
        | project lo = start_ts - 30s, hi = expected_end_ts + 120s;
    let n_inj = toscalar(inj | count);
    fires
    | extend matched = tobool(toscalar(
        // placeholder, replaced below
        print x=false))
    | summarize fires = count()
    """
    # Simpler: compute matches with a join in KQL.
    q = f"""
    let gate = {GATE};
    let fires = score_multivariate_onnx_lookback('{model}', '{mach}', 1s, {LB}, {thr})
        | where activity >= gate and score > {thr}
        | project window_end;
    let inj = injected_anomalies
        | where machine_id == '{mach}' and start_ts > ago({LB})
        | project lo = start_ts - 30s, hi = expected_end_ts + 120s;
    fires
    | extend dummy = 1
    | join kind=leftouter (inj | extend dummy = 1) on dummy
    | summarize hit = max(iff(window_end between (lo .. hi), 1, 0)) by window_end
    | summarize fires = count(), tp = countif(hit == 1), fp = countif(hit == 0)
    """
    try:
        t = c.execute(db, q).primary_results[0]
        rows = list(t)
        if rows:
            r = rows[0]
            fires, tp, fp = r["fires"], r["tp"], r["fp"]
        else:
            fires = tp = fp = 0
        n_inj = int(scalar(
            f"injected_anomalies | where machine_id=='{mach}' and "
            f"start_ts>ago({LB}) | count"))
        print(f"{mach:8} {thr:>7.3f} {fires:>6} {tp:>4} {fp:>4} {n_inj:>8}")
    except Exception as e:
        print(f"{mach:8} {thr:>7.3f} ERROR: {str(e)[:80]}")
